"""Read Codex rollout records as data; preserve original evidence line numbers."""
import hashlib
import itertools
import json
import os
import re
from collections import Counter
from pathlib import Path


def local_sources():
    root = Path(os.environ.get('CODEX_HOME', str(Path.home()/'.codex'))).expanduser()
    paths = [root/name for name in ('sessions', 'archived_sessions') if (root/name).exists()]
    return paths or [root/'sessions']


def text_blocks(value):
    """Do not count encoded screenshots as text or expose their data in excerpts."""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, RecursionError):
            return [{'type': 'text', 'text': value}]
        if not isinstance(parsed, list):
            return [{'type': 'text', 'text': value}]
        value = parsed
    if not isinstance(value, list):
        return []
    return [({'type': 'text', 'text': b['text']} if b.get('type') in ('text', 'input_text', 'output_text')
             and isinstance(b.get('text'), str) else {'type': 'image'}) for b in value if isinstance(b, dict)]


def excerpt(row):
    p = row.get('payload', {})
    if not isinstance(p, dict):
        return ''
    kind = p.get('type', row.get('type'))
    if row.get('type') == 'token_usage_record':
        return json.dumps({'usage': p.get('usage')}, ensure_ascii=False)
    if kind == 'message':
        blocks = text_blocks(p.get('content'))
    elif kind in ('function_call_output', 'custom_tool_call_output'):
        blocks = text_blocks(p.get('output'))
    elif kind in ('function_call', 'custom_tool_call'):
        return str(p.get('name', ''))+'\n'+str(p.get('arguments', p.get('input', '')))
    elif kind == 'token_count':
        return json.dumps(p.get('info'), ensure_ascii=False)
    else:
        return '[Codex '+str(kind)+': 本文なし]'
    return '\n'.join(b.get('text', '[非テキスト要素]') for b in blocks)


def poll_call(name, args):
    """Conservative, non-executing recognizer. Dynamic JS and mixed work stay unknown."""
    if name not in ('sleep','clock.sleep','wait','functions.wait','write_stdin','functions.write_stdin','exec','functions.exec'):
        return False
    name = name.rsplit('.', 1)[-1]
    if isinstance(args, str):
        try:
            obj = json.loads(args)
        except (ValueError, RecursionError):
            obj = None
    else:
        obj = args
    if name in ('sleep', 'wait') and isinstance(obj, dict):
        return (set(obj) <= {'duration_ms'} and isinstance(obj.get('duration_ms'), int)) if name == 'sleep' else (
            set(obj) <= {'cell_id', 'yield_time_ms', 'max_tokens', 'terminate'} and bool(obj.get('cell_id')) and not obj.get('terminate'))
    if name == 'write_stdin' and isinstance(obj, dict):
        return set(obj) <= {'session_id', 'chars', 'yield_time_ms', 'max_output_tokens'} and bool(obj.get('session_id')) and obj.get('chars', '') == ''
    if name != 'exec' or not isinstance(args, str):
        return False
    # Only literal text(await tools.write_stdin({...})); statements are accepted.
    code = re.sub(r'^\s*// @exec:[^\n]*\n', '', args)
    pattern = r'\s*text\s*\(\s*await\s+tools\.write_stdin\s*\(\s*(\{[^{}]*\})\s*\)\s*\)\s*;?'
    pos, count = 0, 0
    while code[pos:].strip():
        match = re.match(pattern, code[pos:])
        if not match:
            return False
        literal = re.sub(r'([,{]\s*)([a-zA-Z_][a-zA-Z_0-9]*)\s*:', r'\1"\2":', match.group(1))
        try:
            obj = json.loads(literal)
        except ValueError:
            return False
        if not poll_call('write_stdin', obj):
            return False
        count += 1
        pos += match.end()
    return count > 0


def records(path, stats, reader, provider='auto'):
    """Modern response records take precedence over legacy cumulative notifications."""
    from .audit import number, safe_model
    stream = reader(path, stats)
    first = next(stream, None)
    if first is None:
        return
    native = first[1].get('type') in ('session_meta', 'turn_context', 'event_msg', 'response_item', 'token_usage_record')
    if not native:
        for line, row in itertools.chain([first], stream):
            if provider != 'codex':
                row = dict(row)
                row.pop('_codex', None)
                yield line, row
        return
    if provider == 'claude':
        stats['filtered_codex_files'] += 1
        return
    # A bounded first pass determines the protocol; do not retain large transcripts.
    modern, inherited_metadata, canonical = False, False, None
    for _, row in itertools.chain([first], stream):
        modern |= row.get('type') == 'token_usage_record'
        p = row.get('payload')
        if row.get('type') == 'session_meta' and isinstance(p, dict):
            raw = p.get('id')
            if isinstance(raw, str) and raw:
                if canonical is None:
                    canonical = raw
                elif raw != canonical:
                    inherited_metadata = True
    stats['codex_files'] += 1
    scan_stats = Counter()
    sid = parent = None
    model, mode, speed = 'unknown', None, None
    pending, previous, reset, epoch = [], None, False, 0
    for line, row in reader(path, scan_stats):
        kind, p = row.get('type'), row.get('payload', {})
        if not isinstance(p, dict):
            continue
        if kind == 'session_meta':
            if sid is not None:
                stats['codex_inherited_metadata_ignored'] += 1
                continue
            raw = p.get('id')
            sid = raw if isinstance(raw, str) and raw else 'file:'+str(path)
            source = p.get('source')
            source = source if isinstance(source, dict) else {}
            child = source.get('subagent', {})
            child = child if isinstance(child, dict) else {}
            spawn = child.get('thread_spawn', {})
            spawn = spawn if isinstance(spawn, dict) else {}
            parent = spawn.get('parent_thread_id')
            parent = parent if isinstance(parent, str) and parent and parent != sid else None
            continue
        if kind == 'turn_context':
            model = safe_model(p.get('model'))
            if not model.startswith('gpt-'):
                model = 'unknown'
            mode = p.get('service_tier')
            speed = p.get('speed')
            continue
        if sid is None:
            sid = 'file:'+str(path)
            stats['codex_missing_session_header'] += 1
        meta = {'parent': 'codex:'+parent if parent else None, 'subagent': bool(parent)}
        base = {'timestamp': row.get('timestamp'), 'sessionId': 'codex:'+sid,
                'uuid': 'codex:'+sid+':'+str(line), '_codex': meta}
        usage = None
        if kind == 'token_usage_record':
            if p.get('thread_id') not in (None, sid):
                stats['codex_foreign_response_ignored'] += 1
                pending = []
                continue
            usage, rid = p.get('usage'), p.get('response_id')
            if not isinstance(rid, str) or not rid:
                stats['codex_missing_response_id'] += 1
                pending = []
                continue
            rid = 'codex:'+rid
            if not isinstance(usage, dict):
                stats['codex_invalid_usage'] += 1
                pending = []
                continue
        elif kind == 'event_msg' and p.get('type') == 'token_count':
            if modern:
                stats['codex_cumulative_notifications_ignored'] += 1
                continue
            # Legacy notifications have no response/thread ownership. A copied
            # parent prefix cannot safely be separated from the child's usage.
            if inherited_metadata:
                stats['codex_legacy_fork_usage_ambiguous'] += 1
                pending = []
                continue
            info = p.get('info')
            if not isinstance(info, dict):
                continue
            total, last = info.get('total_token_usage'), info.get('last_token_usage')
            if not isinstance(total, dict) or not isinstance(last, dict):
                stats['codex_legacy_ambiguous'] += 1
                pending = []
                continue
            keys = ('input_tokens', 'cached_input_tokens', 'output_tokens')
            current = tuple(number(total.get(k)) for k in keys)
            values = tuple(number(last.get(k)) for k in keys)
            if current == previous and not reset:
                stats['codex_legacy_notifications_repeated'] += 1
                continue
            comparable = all(v is not None for v in current+values)
            accepted = comparable and (current == values if previous is None or reset else
                all(a is not None and b-a == v for a,b,v in zip(previous,current,values)))
            previous, reset = current, False
            if not accepted:
                stats['codex_legacy_ambiguous'] += 1
                pending = []
                continue
            usage = last
            rid = 'codex:legacy:'+sid+':'+hashlib.sha256(json.dumps([row.get('timestamp'),current,last,epoch],sort_keys=True).encode()).hexdigest()
            stats['codex_legacy_responses'] += 1
        if usage is not None:
            if not isinstance(usage, dict):
                stats['codex_invalid_usage'] += 1
                pending = []
                continue
            inp, cached, out = (number(usage.get(k)) for k in ('input_tokens','cached_input_tokens','output_tokens'))
            write = number(usage.get('cache_write_input_tokens'))
            reasoning = number(usage.get('reasoning_output_tokens'))
            total = number(usage.get('total_tokens'))
            invalid = (inp is None or out is None or (cached or 0)+(write or 0)>inp
                       or (reasoning is not None and reasoning>out) or (total is not None and total!=inp+out))
            if invalid:
                stats['codex_invalid_usage'] += 1
                pending = []
                continue
            complete = cached is not None and write is not None
            if not complete:
                stats['codex_incomplete_cache_counters'] += 1
            meta.update(complete=complete, reasoning=reasoning, polling=bool(pending) and all(x[0] for x in pending),
                        polling_lines=[x[1] for x in pending[:3]] if pending and all(x[0] for x in pending) else [])
            pending = []
            converted = {'input_tokens':inp-(cached or 0)-(write or 0),'cache_read_input_tokens':cached or 0,
                         'cache_creation_input_tokens':write or 0,'output_tokens':out,
                         'service_tier':'standard' if mode == 'default' else mode,'speed':speed}
            yield line,{**base,'type':'assistant','message':{'id':rid,'model':model,'usage':converted,'content':[]}}
        elif kind == 'compacted':
            reset, pending = True, []
            epoch += 1
            yield line,{**base,'type':'system','subtype':'compact_boundary'}
        elif kind == 'response_item':
            subtype = p.get('type')
            if subtype == 'message' and p.get('role') in ('user','assistant'):
                blocks = text_blocks(p.get('content'))
                text = '\n'.join(b.get('text','') for b in blocks)
                ismeta = text.startswith(('# AGENTS.md instructions','<environment_context>','<codex_internal_context','<INSTRUCTIONS>'))
                yield line,{**base,'type':p['role'],'isMeta':ismeta,'message':{'content':blocks}}
            elif subtype in ('function_call','custom_tool_call'):
                args = p.get('arguments',p.get('input',''))
                pending.append((poll_call(p.get('name'),args),line))
                try:
                    obj = json.loads(args) if isinstance(args,str) else args
                except (ValueError, RecursionError):
                    obj = {'script':args}
                if not isinstance(obj,dict):
                    obj = {'value':obj}
                yield line,{**base,'type':'assistant','message':{'content':[{'type':'tool_use','id':p.get('call_id'),
                    'name':p.get('name'),'input':obj}]}}
            elif subtype in ('function_call_output','custom_tool_call_output'):
                yield line,{**base,'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':p.get('call_id'),
                    'content':text_blocks(p.get('output')),'is_error':'unknown'}]}}

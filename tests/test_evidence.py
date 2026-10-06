import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from claude_history_audit.audit import audit, load_prices
from claude_history_audit.report import write_reports
from claude_history_audit.evidence import inspect_item, finalize, load, TEXT_LIMIT
from test_insights import resume_rows
from test_diagnosis import ROOT


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.source=self.root/'input.jsonl'
        rows=resume_rows()
        for r in rows:
            r['message']['content']=[{'type':'text','text':'PRIVATE_CANARY_DO_NOT_EXECUTE '+('x'*5000)}]
        self.source.write_text(''.join(json.dumps(r)+'\n' for r in rows),encoding='utf-8')
        report, local=audit([self.source],deep=True,prices=load_prices(ROOT/'app/claude_history_audit/reference_prices.json'))
        self.out=write_reports(self.root/'out',report,local)

    def tearDown(self):
        self.tmp.cleanup()

    def reviewed(self, status='supported_hypothesis'):
        notes=load(self.out/'review-notes.private.json')
        for item in notes['items']:
            packet=inspect_item(self.out,item['id'])
            item.update(status=status,evidence=[e['focus'] for e in packet['excerpts'] if e['available']],
                        observation='合成履歴の観測内容',interpretation='失効と整合するが未確定',alternatives='入力変更の可能性も残る',
                        action='この作業で休憩前の引継ぎを試す',validation='同じ合格基準で総費用と修正回数を比較する')
        return notes

    def test_inspection_is_bounded_private_and_input_unchanged(self):
        before=self.source.read_bytes()
        plan=load(self.out/'review-plan.json')
        packet=inspect_item(self.out,plan['items'][0]['id'])
        self.assertIn('PRIVATE_CANARY',json.dumps(packet))
        for e in packet['excerpts']:
            self.assertLessEqual(len(e['records']),3)
            self.assertTrue(all(len(r['untrusted_content'])<=TEXT_LIMIT for r in e['records']))
        for name in ('report.json','summary.md','report.html','review-plan.json'):
            self.assertNotIn('PRIVATE_CANARY',(self.out/name).read_text())
        self.assertEqual(self.source.read_bytes(),before)

    def test_pending_missing_and_duplicate_reviews_cannot_finalize(self):
        with self.assertRaises(ValueError):
            finalize(self.out,load(self.out/'review-notes.private.json'))
        notes=self.reviewed()
        removed=copy.deepcopy(notes);removed['items'].pop()
        with self.assertRaises(ValueError):finalize(self.out,removed)
        duplicate=copy.deepcopy(notes);duplicate['items'].append(duplicate['items'][0])
        with self.assertRaises(ValueError):finalize(self.out,duplicate)

    def test_complete_first_pass_writes_private_diagnosis(self):
        result=finalize(self.out,self.reviewed())
        self.assertEqual(result['status'],'reviewed')
        self.assertGreater(result['reviewed_items'],0)
        self.assertTrue((self.out/'diagnosis-reviewed.private.md').exists())
        if os.name!='nt':
            self.assertEqual((self.out/'diagnosis-reviewed.private.md').stat().st_mode & 0o777,0o600)

    def test_unresolved_requires_inspection_and_citation_when_available(self):
        notes=self.reviewed('unresolved')
        self.assertEqual(finalize(self.out,notes)['status'],'reviewed_with_unknowns')
        notes['items'][0]['evidence']=[]
        with self.assertRaises(ValueError):finalize(self.out,notes)

    def test_missing_files_are_explicit_unresolved_not_verified(self):
        self.source.unlink()
        notes=self.reviewed('unresolved')
        self.assertTrue(all(not i['evidence'] for i in notes['items']))
        self.assertEqual(finalize(self.out,notes)['status'],'reviewed_with_unknowns')
        notes['items'][0]['status']='supported_hypothesis'
        with self.assertRaises(ValueError):finalize(self.out,notes)

    def test_changed_source_invalidates_inspection(self):
        notes=self.reviewed()
        self.source.write_text(self.source.read_text().replace('PRIVATE_CANARY','CHANGED_CANARY'))
        with self.assertRaisesRegex(ValueError,'変更・削除'):finalize(self.out,notes)

    def test_changed_before_inspection_is_not_available_as_original_evidence(self):
        self.source.write_text(self.source.read_text().replace('PRIVATE_CANARY','CHANGED_CANARY'))
        plan=load(self.out/'review-plan.json')
        packet=inspect_item(self.out,plan['items'][0]['id'])
        self.assertTrue(all(not e['available'] for e in packet['excerpts']))

    def test_appending_history_does_not_invalidate_existing_evidence(self):
        notes=self.reviewed()
        with self.source.open('a') as f:f.write('{}\n')
        self.assertEqual(finalize(self.out,notes)['status'],'reviewed')

    def test_cross_report_or_fabricated_evidence_rejected(self):
        notes=self.reviewed()
        notes['report_digest']='wrong'
        with self.assertRaises(ValueError):finalize(self.out,notes)
        notes=self.reviewed()
        notes['items'][0]['evidence']=[{'file':'file-fake','line':999}]
        with self.assertRaises(ValueError):finalize(self.out,notes)

    def test_failed_recheck_does_not_leave_old_completion_marker(self):
        notes=self.reviewed()
        finalize(self.out,notes)
        notes['items'][0]['action']=''
        with self.assertRaises(ValueError):finalize(self.out,notes)
        self.assertEqual(load(self.out/'review-status.json')['status'],'review_failed')

    def test_neighbor_lines_can_be_cited_only_when_returned(self):
        notes=self.reviewed()
        item=notes['items'][0]
        packet=inspect_item(self.out,item['id'])
        ref=packet['excerpts'][0]['records'][0]
        item['evidence']=[{'file':ref['file'],'line':ref['line']}]
        self.assertEqual(finalize(self.out,notes)['status'],'reviewed')

    def test_no_receipt_or_empty_action_rejected(self):
        notes=self.reviewed()
        notes['items'][0]['action']=' '
        with self.assertRaises(ValueError):finalize(self.out,notes)
        notes=self.reviewed()
        (self.out/'review-receipts.private'/(notes['items'][0]['id']+'.json')).unlink()
        with self.assertRaises(OSError):finalize(self.out,notes)

    def test_symlink_history_not_read_during_review(self):
        backup=self.root/'backup.jsonl'
        self.source.rename(backup)
        self.source.symlink_to(backup)
        plan=load(self.out/'review-plan.json')
        packet=inspect_item(self.out,plan['items'][0]['id'])
        self.assertTrue(all(not e['available'] for e in packet['excerpts']))


if __name__=='__main__':unittest.main()

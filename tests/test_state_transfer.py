"""Committed backend history must not bypass the HUMAN frozen-transfer gate."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REFERENCES = Path(__file__).resolve().parents[1] / '.ai-rulez/skills/infra/references'


@unittest.skipUnless(os.name == 'posix' and shutil.which('jq'), 'POSIX shell and jq required')
class StateTransferTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.checkout = Path(temporary.name)
        self.execute_git('init', '-q')
        (self.checkout / 'terraform/github').mkdir(parents=True)
        (self.checkout / 'README.md').write_text('state_transfers: {}\n')
        self.backend = self.checkout / 'terraform/github/backend.tf'
        self.environment = dict(os.environ)

    def execute_git(self, *arguments: str) -> str:
        result = subprocess.run(['git', *arguments], cwd=self.checkout, capture_output=True,
                                text=True, check=True)
        return result.stdout.strip()

    def commit_files(self, date: str | None = None) -> None:
        self.execute_git('add', 'terraform', 'README.md')
        environment = dict(os.environ)
        if date is not None:
            environment.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
        subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.com',
                        'commit', '-qm', 'cutover fixture'], cwd=self.checkout, env=environment,
                       capture_output=True, text=True, check=True)

    def prepare_legacy_migration(self, cutover_date: str) -> dict[str, object]:
        self.backend.write_text('terraform { cloud { organization = "acme" } }\n')
        self.commit_files('2024-01-01T00:00:00Z')
        self.backend.write_text('terraform { backend "gcs" { bucket = "acme-state" } }\n')
        self.commit_files(cutover_date)
        cutover = self.execute_git('rev-parse', 'HEAD')
        (self.checkout / 'terraform/github/main.tf').write_text('locals {}\n')
        self.commit_files()
        return {'legacy': True, 'cutover_commit': cutover,
                'migration': 'https://github.com/acme/infra/issues/1',
                'backend_blob': self.execute_git('hash-object', str(self.backend))}

    def check_transfer(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(['sh', str(REFERENCES / 'checks/state-transfer.sh'), 'github'],
                              cwd=self.checkout, env=self.environment, capture_output=True, text=True)

    def prepare_migration(self) -> dict[str, object]:
        self.backend.write_text('terraform { cloud { organization = "acme" } }\n')
        self.commit_files()
        self.backend.write_text('terraform { backend "gcs" { bucket = "acme-state" } }\n')
        self.commit_files()
        return {'source_backend': 'hcp', 'workspace_id': 'ws-public123', 'serial': 42,
                'lineage': 'public-lineage', 'state_sha256': 'a' * 64,
                'hcp_locked_before_pull': True, 'destination_verified': True,
                'backend_blob': self.execute_git('hash-object', str(self.backend)),
                'verified_at': '2025-01-02T00:00:00Z'}

    def test_fresh_leaf_needs_no_migration_record(self) -> None:
        self.backend.write_text('terraform { backend "gcs" { bucket = "acme-state" } }\n')
        self.commit_files()
        self.assertEqual(self.check_transfer().returncode, 0)

    def test_backend_only_cutover_cannot_skip_transfer_evidence(self) -> None:
        self.prepare_migration()
        result = self.check_transfer()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('record the frozen, verified HUMAN HCP transfer', result.stderr)

    def test_current_record_passes_but_incomplete_or_retargeted_records_fail(self) -> None:
        transfer = self.prepare_migration()
        self.environment['STATE_TRANSFERS'] = json.dumps({'github': transfer})
        self.assertEqual(self.check_transfer().returncode, 0)
        for invalid_fields in ({'hcp_locked_before_pull': False}, {'destination_verified': False},
                               {'backend_blob': 'old-backend'}, {'verified_at': '2999-01-02T00:00:00Z'},
                               {'state_sha256': ''}, {'serial': -1}):
            with self.subTest(fields=invalid_fields):
                self.environment['STATE_TRANSFERS'] = json.dumps({'github': {**transfer, **invalid_fields}})
                self.assertEqual(self.check_transfer().returncode, 1)
        self.environment['STATE_TRANSFERS'] = json.dumps({'github': transfer})
        self.backend.write_text('terraform { backend "gcs" { bucket = "another-destination" } }\n')
        self.commit_files()
        self.assertEqual(self.check_transfer().returncode, 1)

    def test_backend_in_an_unhashed_file_cannot_use_an_unchanged_attestation(self) -> None:
        transfer = self.prepare_migration()
        (self.checkout / 'terraform/github/versions.tf').write_text(self.backend.read_text())
        self.backend.write_text('# backend moved elsewhere\n')
        self.commit_files()
        transfer['backend_blob'] = self.execute_git('hash-object', str(self.backend))
        self.environment['STATE_TRANSFERS'] = json.dumps({'github': transfer})
        self.assertEqual(self.check_transfer().returncode, 1)

    def test_json_backend_outside_the_reviewed_file_is_rejected(self) -> None:
        transfer = self.prepare_migration()
        (self.checkout / 'terraform/github/override.tf.json').write_text(json.dumps({
            'terraform': {'backend': {'gcs': {'bucket': 'another-destination'}}}}))
        self.commit_files()
        self.environment['STATE_TRANSFERS'] = json.dumps({'github': transfer})
        self.assertEqual(self.check_transfer().returncode, 1)

    def test_resource_json_does_not_invalidate_the_reviewed_backend(self) -> None:
        transfer = self.prepare_migration()
        (self.checkout / 'terraform/github/resources.tf.json').write_text(json.dumps({
            'resource': {'null_resource': {'example': {}}}}))
        self.commit_files()
        self.environment['STATE_TRANSFERS'] = json.dumps({'github': transfer})
        self.assertEqual(self.check_transfer().returncode, 0)

    def test_backend_declared_outside_the_hashed_file_fails(self) -> None:
        transfer = self.prepare_migration()
        self.environment['STATE_TRANSFERS'] = json.dumps({'github': transfer})
        leaf = self.checkout / 'terraform/github'
        for name, text in (
                ('versions.tf', 'terraform {\n  backend "gcs" { bucket = "another-destination" }\n}\n'),
                ('backend_override.tf', 'terraform {\n  backend "gcs" {\n    prefix = "elsewhere"\n  }\n}\n'),
                ('versions.tf', 'terraform {\n  cloud {\n  }\n}\n'),
                ('main.tf.json', '{"terraform": {"backend": {"gcs": {"bucket": "another-destination"}}}}\n')):
            with self.subTest(file=name):
                (leaf / name).write_text(text)
                result = self.check_transfer()
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(f'terraform/github/{name}', result.stderr)
                (leaf / name).unlink()
        (leaf / 'versions.tf').write_text('# backend "gcs" lives in backend.tf\nterraform {}\n')
        self.assertEqual(self.check_transfer().returncode, 0)
        self.backend.write_text('terraform {}\n')
        self.commit_files()
        self.environment['STATE_TRANSFERS'] = json.dumps(
            {'github': {**transfer, 'backend_blob': self.execute_git('hash-object', str(self.backend))}})
        self.assertEqual(self.check_transfer().returncode, 1)

    def prepare_json_migration(self, backend_shape: dict[str, object]) -> None:
        json_backend = self.checkout / 'terraform/github/versions.tf.json'
        json_backend.write_text(json.dumps({'terraform': backend_shape}).replace('"cloud"', '"\\u0063loud"'))
        self.backend.write_text('terraform {}\n')
        self.commit_files()
        json_backend.unlink()
        self.backend.write_text('terraform { backend "gcs" { bucket = "acme-state" } }\n')
        self.commit_files()

    def test_historical_json_cloud_cannot_be_treated_as_fresh(self) -> None:
        self.prepare_json_migration({'cloud': {'organization': 'acme'}})
        self.assertEqual(self.check_transfer().returncode, 1)

    def test_historical_json_remote_cannot_be_treated_as_fresh(self) -> None:
        self.prepare_json_migration({'backend': {'remote': {'organization': 'acme'}}})
        self.assertEqual(self.check_transfer().returncode, 1)

    def test_commented_hcp_history_cannot_be_treated_as_fresh(self) -> None:
        """Codex repro: HCL comments between tokens hide the committed remote."""
        for history_text in ('terraform {\n  backend /* legacy HCP */ "remote" { organization = "acme" } }\n}\n',
                             'terraform {\n  cloud /* legacy HCP */ { organization = "acme" } }\n}\n'):
            with self.subTest(history=history_text):
                hidden = self.checkout / 'terraform/github/versions.tf'
                hidden.write_text(history_text)
                self.commit_files()
                hidden.unlink()
                self.backend.write_text('terraform { backend "gcs" { bucket = "acme-state" } }\n')
                self.commit_files()
                result = self.check_transfer()
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn('record the frozen, verified HUMAN HCP transfer', result.stderr)

    def test_current_comments_do_not_distort_the_reviewed_layout(self) -> None:
        """Comments never create false declarations nor hide reviewed ones."""
        transfer = self.prepare_migration()
        self.environment['STATE_TRANSFERS'] = json.dumps({'github': transfer})
        self.backend.write_text('terraform {\n  backend /* reviewed in the cutover PR */ "gcs" { bucket = "acme-state" }\n}\n')
        self.commit_files()
        transfer['backend_blob'] = self.execute_git('hash-object', str(self.backend))
        self.environment['STATE_TRANSFERS'] = json.dumps({'github': transfer})
        self.assertEqual(self.check_transfer().returncode, 0)

    def test_old_cutover_passes_with_a_legacy_record(self) -> None:
        legacy = self.prepare_legacy_migration('2024-02-01T00:00:00Z')
        self.environment['STATE_TRANSFERS'] = json.dumps({'github': legacy})
        result = self.check_transfer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('LEGACY state transfer for github', result.stderr)

    def test_legacy_record_cannot_skip_evidence_for_a_fresh_cutover(self) -> None:
        now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        legacy = self.prepare_legacy_migration(now)
        self.environment['STATE_TRANSFERS'] = json.dumps({'github': legacy})
        result = self.check_transfer()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('landed less than 30 days ago', result.stderr)

    def test_legacy_record_must_match_history_backend_and_migration(self) -> None:
        legacy = self.prepare_legacy_migration('2024-02-01T00:00:00Z')
        hcp_commit = self.execute_git('rev-list', '--max-parents=0', 'HEAD')
        for invalid_fields in ({'cutover_commit': hcp_commit}, {'cutover_commit': 'HEAD'},
                               {'backend_blob': 'old-backend'}, {'migration': ''},
                               {'migration': 'not a url'},
                               {'migration': 'https://github.com/acme/infra/issues/1\n'},
                               {'legacy': 'true'}):
            with self.subTest(fields=invalid_fields):
                self.environment['STATE_TRANSFERS'] = json.dumps({'github': {**legacy, **invalid_fields}})
                result = self.check_transfer()
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertNotIn('LEGACY', result.stderr)

    def test_legacy_record_cannot_cover_a_leaf_still_on_hcp(self) -> None:
        self.backend.write_text('terraform { backend "gcs" { bucket = "acme-state" } }\n')
        self.commit_files('2024-01-01T00:00:00Z')
        self.backend.write_text('terraform { cloud { organization = "acme" } }\n')
        self.commit_files('2024-02-01T00:00:00Z')
        self.environment['STATE_TRANSFERS'] = json.dumps({'github': {
            'legacy': True, 'cutover_commit': self.execute_git('rev-list', '--max-parents=0', 'HEAD'),
            'migration': 'https://github.com/acme/infra/issues/1',
            'backend_blob': self.execute_git('hash-object', str(self.backend))}})
        result = self.check_transfer()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertNotIn('LEGACY', result.stderr)

    def test_backdated_branch_merged_today_is_not_a_legacy_cutover(self) -> None:
        self.backend.write_text('terraform { cloud { organization = "acme" } }\n')
        self.commit_files('2024-01-01T00:00:00Z')
        main = self.execute_git('rev-parse', '--abbrev-ref', 'HEAD')
        self.execute_git('switch', '-q', '-c', 'old-branch')
        self.backend.write_text('terraform { backend "gcs" { bucket = "acme-state" } }\n')
        self.commit_files('2024-02-01T00:00:00Z')
        branch_commit = self.execute_git('rev-parse', 'HEAD')
        self.execute_git('switch', '-q', main)
        # main moves on outside the leaf, so the merge is TREESAME to the branch for
        # this path and plain path-limited history would hide it.
        (self.checkout / 'README.md').write_text('state_transfers: {}\nupdated: true\n')
        self.commit_files('2024-03-01T00:00:00Z')
        self.execute_git('-c', 'user.name=Test', '-c', 'user.email=test@example.com',
                         'merge', '-q', '--no-ff', '-m', 'merge today', 'old-branch')
        blob = self.execute_git('hash-object', str(self.backend))
        for cutover in (branch_commit, self.execute_git('rev-parse', 'HEAD')):
            with self.subTest(cutover=cutover):
                self.environment['STATE_TRANSFERS'] = json.dumps({'github': {
                    'legacy': True, 'cutover_commit': cutover, 'backend_blob': blob,
                    'migration': 'https://github.com/acme/infra/issues/1'}})
                result = self.check_transfer()
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertNotIn('LEGACY', result.stderr)

    def test_legacy_record_survives_moving_the_same_backend_block(self) -> None:
        legacy = self.prepare_legacy_migration('2024-02-01T00:00:00Z')
        versions = self.checkout / 'terraform/github/versions.tf'
        versions.write_text('terraform { backend "gcs" { bucket = "acme-state" } }\n')
        self.backend.unlink()
        self.commit_files('2024-04-01T00:00:00Z')
        versions.write_text('terraform {}\n')
        self.backend.write_text('# moved here\nterraform {\n  backend "gcs" {\n    bucket = "acme-state"\n  }\n}\n')
        self.commit_files()
        legacy['backend_blob'] = self.execute_git('hash-object', str(self.backend))
        self.environment['STATE_TRANSFERS'] = json.dumps({'github': legacy})
        result = self.check_transfer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('LEGACY', result.stderr)

    def test_legacy_record_cannot_cover_a_fresh_backend_retarget(self) -> None:
        legacy = self.prepare_legacy_migration('2024-02-01T00:00:00Z')
        self.backend.write_text('terraform { backend "gcs" { bucket = "another-state" } }\n')
        self.commit_files()
        legacy['backend_blob'] = self.execute_git('hash-object', str(self.backend))
        self.environment['STATE_TRANSFERS'] = json.dumps({'github': legacy})
        result = self.check_transfer()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('backend changed after cutover', result.stderr)

    def test_legacy_record_cannot_cover_a_changed_endpoint_url(self) -> None:
        self.backend.write_text('terraform { cloud { organization = "acme" } }\n')
        self.commit_files('2024-01-01T00:00:00Z')
        old = ('terraform {\n  backend "s3" {\n    bucket = "state"\n'
               '    endpoints = { s3 = "https://account-a.r2.example.com" } // R2\n  }\n}\n')
        self.backend.write_text(old)
        self.commit_files('2024-02-01T00:00:00Z')
        cutover = self.execute_git('rev-parse', 'HEAD')
        self.backend.write_text(old.replace('account-a', 'account-b'))
        self.commit_files()
        self.environment['STATE_TRANSFERS'] = json.dumps({'github': {
            'legacy': True, 'cutover_commit': cutover,
            'migration': 'https://github.com/acme/infra/issues/1',
            'backend_blob': self.execute_git('hash-object', str(self.backend))}})
        result = self.check_transfer()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('backend changed after cutover', result.stderr)

    def legacy_record_for_backend(self, cutover_backend: str, current_backend: str) -> subprocess.CompletedProcess[str]:
        self.backend.write_text('terraform { cloud { organization = "acme" } }\n')
        self.commit_files('2024-01-01T00:00:00Z')
        self.backend.write_text(cutover_backend)
        self.commit_files('2024-02-01T00:00:00Z')
        cutover = self.execute_git('rev-parse', 'HEAD')
        self.backend.write_text(current_backend)
        (self.checkout / 'terraform/github/main.tf').write_text('locals {}\n')
        self.commit_files()
        self.environment['STATE_TRANSFERS'] = json.dumps({'github': {
            'legacy': True, 'cutover_commit': cutover,
            'migration': 'https://github.com/acme/infra/issues/1',
            'backend_blob': self.execute_git('hash-object', str(self.backend))}})
        return self.check_transfer()

    def test_legacy_record_refuses_a_heredoc_backend(self) -> None:
        heredoc = ('terraform {\n  backend "s3" {\n    bucket = "state"\n'
                   '    endpoints = { s3 = <<EOT\nhttps://account-a.r2.example.com\nEOT\n    }\n  }\n}\n')
        result = self.legacy_record_for_backend(heredoc, heredoc.replace('account-a', 'account-b'))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('no heredoc', result.stderr)

    def test_legacy_record_accepts_a_comment_before_the_backend_label(self) -> None:
        commented = 'terraform { backend /* note */ "gcs" { bucket = "acme-state" } }\n'
        result = self.legacy_record_for_backend(commented, commented)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('LEGACY', result.stderr)

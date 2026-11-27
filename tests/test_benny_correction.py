import json
from pathlib import Path
from shutil import copytree
import unittest

import test_benny
import test_benny_reproduce
from workbench import benny, benny_correction, benny_notice, benny_reproduce, cli


class BennyCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_benny_reproduce.BennyReproductionTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.root = self.fixture.root
        self.source = self.fixture.source
        original = self.fixture.runtime
        self.runtime = {'repo': self.fixture.repo, 'image': 'runtime:local', 'manifest': original.manifest,
                        'helpers': original.helpers, 'journey': 'main', 'oracle': original.oracle,
                        'oracle_marker': 'ORACLE_PASS', 'oracle_failure_marker': 'ORACLE_FAIL', 'memory_mb': 512}
        variant_helpers = self.root/'variant-helpers'
        copytree(original.helpers, variant_helpers)
        (variant_helpers/'main.sh').write_text('# approved clean startup\nprintf MAIN_PASS\n')
        self.variant_manifest = self.root/'variant.json'
        contract = json.loads(original.manifest.read_text())
        contract['features'][0]['drive'] = 'Use a clean initial setup before reading the same item.'
        cli.write(self.variant_manifest, contract)
        self.variants = {'clean-setup': {'manifest': self.variant_manifest, 'helpers': variant_helpers,
                                         'oracle': original.oracle}}

    def freeze(self):
        return benny_correction.freeze(self.root/'correction-options.json', self.variants, self.runtime)

    def ready(self):
        archive = self.freeze()
        proof = self.fixture.reproduce(claim_seconds=60, out=self.root/'reproduction')
        initial_file = self.root/'reproduction/result.json'
        initial = {'path': str(initial_file), 'sha256': cli.digest(initial_file)}
        notice = self.fixture.publish_notice(initial)
        row = benny_notice.confirmed(notice, reproduction=initial, proof=proof)
        ready_at = benny_reproduce._claim_ready_at(proof, row)
        return {'initial': initial, 'notice': notice, 'archive': archive, 'slack': self.source.slack,
                'ready_at': ready_at, 'out': self.root/'correction/source'}

    def add_correction(self, args, *, offset=-1, user='U123PERSON', text='Correction: setup: clean-setup'):
        seconds = int(args['ready_at']) + offset
        row = test_benny.reply(500, text, user=user, ts=f'{seconds}.000000')
        self.source.pages[-1].append(row)
        return row

    def test_frozen_recipe_retains_exact_probe_when_live_input_changes(self):
        archive = self.freeze()
        approved = benny_correction.options(archive, 'clean-setup', self.runtime)
        helper = Path(approved['helpers'])/'main.sh'
        self.assertEqual(self.variants['clean-setup']['helpers'].joinpath('main.sh').read_bytes(), helper.read_bytes())
        self.variants['clean-setup']['helpers'].joinpath('main.sh').write_text('Modified live input.')
        self.assertEqual(benny_correction.options(archive, 'clean-setup', self.runtime), approved)
        helper.write_text('Modified retained input.')
        with self.assertRaisesRegex(ValueError, 'probes changed'):
            benny_correction.options(archive, 'clean-setup', self.runtime)

    def test_recipe_cannot_change_observed_behavior_or_original_runtime(self):
        archive = self.freeze()
        with self.assertRaisesRegex(ValueError, 'runtime scope'):
            benny_correction.options(archive, 'clean-setup', {**self.runtime, 'memory_mb': 1024})
        contract = json.loads(self.variant_manifest.read_text())
        contract['features'][0]['observe'] = 'The item can be absent.'
        cli.write(self.variant_manifest, contract)
        with self.assertRaisesRegex(ValueError, 'observable acceptance contract'):
            self.freeze()

    def test_one_approved_reporter_correction_retains_the_original_and_runs_native_probes(self):
        args = self.ready()
        row = self.add_correction(args)
        route = benny_correction.select(**args)
        self.assertEqual(benny_correction.select(**args), route)
        _, record = benny.load_route(route['path'], route['sha256'])
        correction = record['correction']
        self.assertEqual((correction['initial'], correction['notice'], correction['archive']),
                         (args['initial'], args['notice'], args['archive']))
        self.assertEqual(correction['message']['text'], row['text'])
        self.assertEqual(correction['variant'], 'clean-setup')
        corrected_runtime = benny_correction.options(args['archive'], correction['variant'], self.runtime)
        corrected = self.fixture.reproduce(route=route['path'], sha256=route['sha256'],
                                          out=self.root/'correction/reproduction', **corrected_runtime)
        self.assertEqual(corrected['status'], 'reproduced')
        self.assertEqual(cli.digest(Path(args['initial']['path'])), args['initial']['sha256'])
        self.assertEqual(len(self.source.sent), 1)

    def test_hold_when_reporter_or_time_or_setup_cannot_be_confirmed(self):
        args = self.ready()
        self.assertIsNone(benny_correction.select(**args))
        for changes in ({'user': 'U123OTHER'}, {'offset': 1}, {'text': 'Correction: setup: unknown-mode'}):
            with self.subTest(changes=changes):
                self.add_correction(args, **changes)
                with self.assertRaisesRegex(ValueError, 'approved in-window setup correction'):
                    benny_correction.select(**args)
                self.source.pages[-1].pop()
        self.add_correction(args)
        with self.assertRaisesRegex(ValueError, 'original rejection window'):
            benny_correction.select(**{**args, 'ready_at': args['ready_at'] + 5})

    def test_other_source_activity_and_missing_receipt_cannot_admit_a_second_run(self):
        args = self.ready()
        self.add_correction(args)
        self.source.pages[-1].append(test_benny.reply(700, "I'll work on this"))
        with self.assertRaisesRegex(ValueError, 'source changed beyond'):
            benny_correction.select(**args)
        self.source.pages[-1].pop()
        Path(args['notice']['path']).unlink()
        with self.assertRaises(FileNotFoundError):
            benny_correction.select(**args)

    def test_audit_checks_the_original_receipts_and_corrected_frozen_setup(self):
        args = self.ready()
        self.add_correction(args)
        new_route = benny_correction.select(**args)
        _, route = benny.load_route(new_route['path'], new_route['sha256'])
        updated = benny_correction.options(args['archive'], 'clean-setup', self.runtime)
        out = self.root/'correction/reproduction'
        proof = self.fixture.reproduce(route=new_route['path'], sha256=new_route['sha256'], out=out, **updated)
        contract = cli.read(out/'contract.json')
        proof_path = out/'result.json'
        benny_correction.audit(proof_path, proof, contract, route)

        altered = {**contract, 'journey': {**contract['journey'], 'observe': 'Accept any value.'}}
        with self.assertRaisesRegex(ValueError, 'corrected reproduction differs'):
            benny_correction.audit(proof_path, proof, altered, route)
        helper = Path(updated['helpers'])/'main.sh'
        content = helper.read_bytes()
        helper.write_bytes(content + b'\n# tampered')
        with self.assertRaisesRegex(ValueError, 'correction probes changed'):
            benny_correction.audit(proof_path, proof, contract, route)
        helper.write_bytes(content)

        raw = self.root/'reproduction/baseline/main.primary-1.json'
        raw.write_text(raw.read_text() + '\n')
        with self.assertRaisesRegex(ValueError, 'frozen sandbox receipt'):
            benny_correction.audit(proof_path, proof, contract, route)


if __name__ == '__main__':
    unittest.main()

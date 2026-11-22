import json
import unittest

from host import HostBoard, revision, write_steps


class CompactionPreviewTests(unittest.TestCase):
    def preview(self, **configs):
        board = HostBoard(configs, principal='synthetic')
        board.catalog = {p: [{'provider': 'openai-codex', 'model': 'gpt-6.2-sol'}] for p in configs}
        snaps = {p: (json.dumps(doc), doc) for p, doc in configs.items()}
        draft = {'profiles': list(configs), 'targets': ['main'], 'route': 'explicit',
                 'provider': 'openai-codex', 'model': 'gpt-6.2-sol', 'effort': None,
                 'expected_revisions': {p: revision(board.instance, raw) for p, (raw, _) in snaps.items()}}
        result = board.preview(draft, snaps)
        return board, draft, result

    def test_mixed_main_switch_distinguishes_window_from_ceiling(self):
        def profile(c):
            return {'model': {'provider': 'openai-codex', 'default': 'gpt-6.1-sol', 'context_length': 400000},
                    **({'compression': c} if c is not None else {})}
        docs = {'pinned': profile({'threshold_tokens': 350000}), 'unconfigured': profile(None),
                'disabled': profile({'enabled': False, 'threshold_tokens': 350000}),
                'unavailable': profile({'threshold_tokens': {'never_return': 'PRIVATE_FIXTURE'}}),
                'unchanged': profile({'threshold_tokens': 350000})}
        docs['unchanged']['model']['default'] = 'gpt-6.2-sol'
        docs['unconfigured']['model'].pop('context_length')
        board, draft, result = self.preview(**docs)
        rows = {r['profile']: r for r in result['profiles']}
        self.assertEqual(rows['pinned']['compaction_ceiling'], {'status': 'configured', 'tokens': 350000})
        self.assertEqual(rows['unconfigured']['compaction_ceiling'], {'status': 'unconfigured'})
        self.assertIs(rows['unconfigured']['model_window_auto'], True)
        self.assertNotIn('model_window_auto', rows['pinned'])
        self.assertEqual(rows['disabled']['compaction_ceiling'], {'status': 'disabled'})
        self.assertEqual(rows['unavailable']['compaction_ceiling'], {'status': 'unavailable'})
        self.assertNotIn('compaction_ceiling', rows['unchanged'])
        self.assertNotIn('PRIVATE_FIXTURE', json.dumps(result))
        self.assertEqual([c['field'] for c in rows['pinned']['changes']], ['model.default', 'model.context_length'])
        after, changes = board.plan('pinned', docs['pinned'], draft)
        self.assertEqual(after['compression'], docs['pinned']['compression'])
        self.assertEqual(write_steps(changes, after, []), [('model_set', {'scope': 'main', 'provider': 'openai-codex',
                         'model': 'gpt-6.2-sol'}), ('put_config', {'model_context_length': 0})])
        self.assertNotIn('compaction_ceiling', board.consume(draft, result['token'], True)['plans']['pinned'])


if __name__ == '__main__':
    unittest.main()

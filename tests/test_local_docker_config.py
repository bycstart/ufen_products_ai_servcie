"""在临时目录验证配置切换和备份，不修改实际服务配置。"""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile


SCRIPT = Path(__file__).resolve().parents[1] / 'deploy/local/configure.py'
spec = importlib.util.spec_from_file_location('local_docker_config', SCRIPT)
config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(config)


class LocalDockerConfigTests(unittest.TestCase):
    def test_preserves_model_settings_backs_up_originals_and_reuses_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            ai, customer, ecommerce = [source / name for name in ('ai', 'customer', 'ecommerce')]
            local = ai / 'deploy/local'
            frontends = [source / 'user', source / 'admin']
            for root in (ai, customer, ecommerce, *frontends, local):
                root.mkdir(parents=True, exist_ok=True)
            for root in (ai, customer, ecommerce):
                (root / 'pyproject.toml').write_text('[project]\n', encoding='utf-8')
                (root / '.env').write_text('# 原配置\nJWT_SECRET=vm-secret\n', encoding='utf-8')
            original = '# 模型配置\nLLM_API_KEY=keep-model-key\nLLM_BASE_URL=http://model/v1\nJWT_SECRET=vm-secret\n'
            (ai / '.env').write_text(original, encoding='utf-8')
            original_bytes = (ai / '.env').read_bytes()
            with patch.multiple(config, AI=ai, SOURCE=source, LOCAL=local, ECOMMERCE=ecommerce,
                                CUSTOMER=customer, FRONTENDS=frontends), \
                    patch('sys.argv', ['configure.py']):
                config.main()
                first = config.read_values(local / '.env')
                config.main()
                self.assertEqual(config.read_values(local / '.env'), first)
                new = config.read_values(ai / '.env')
                self.assertEqual(new['LLM_API_KEY'], 'keep-model-key')
                self.assertEqual(new['LLM_BASE_URL'], 'http://model/v1')
                self.assertEqual(new['JWT_SECRET'], config.read_values(ecommerce / '.env')['JWT_SECRET'])
                self.assertIn('127.0.0.1:15432', new['AI_DATABASE_URL'])
                self.assertTrue((frontends[0] / '.env.local').exists())
                archives = sorted((ai / 'backups').glob('*.zip'))
                with ZipFile(archives[0]) as archive:
                    self.assertIsNone(archive.testzip())
                    self.assertEqual(archive.read('ai/.env'), original_bytes)
                with patch('sys.argv', ['configure.py', '--check']):
                    config.main()
                    with patch.dict('os.environ', AI_DATABASE_URL='postgresql://old-vm/ai'), \
                            self.assertRaises(SystemExit):
                        config.main()


if __name__ == '__main__':
    unittest.main()

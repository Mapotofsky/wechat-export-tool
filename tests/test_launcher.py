from pathlib import Path
import unittest
from unittest.mock import patch

import voice_service


class LauncherTests(unittest.TestCase):
    def test_voice_uses_local_environment_and_missing_path_has_repair(self):
        self.assertEqual(voice_service.PYTHON, Path(r'D:\efficiency\anaconda3\envs\pytorch\python.exe'))
        info = voice_service.probe('C:/missing environment/python.exe')
        self.assertFalse(info['available'])
        self.assertIn('voice_service.py', info['reason'])
        self.assertEqual(info['python'], 'C:/missing environment/python.exe')

    def test_voice_subprocess_keeps_space_path_as_single_argument(self):
        path = 'C:/voice environment/python.exe'
        with patch.object(Path, 'is_file', return_value=True), patch.object(voice_service.subprocess, 'run') as run:
            run.return_value.stdout = '{"available":false,"models":[]}'
            info = voice_service.probe(path)
        self.assertEqual(run.call_args.args[0][0], path)
        self.assertIn(path, info['reason'])


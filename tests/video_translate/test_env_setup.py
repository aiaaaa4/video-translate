import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "skills" / "video-translate" / "scripts" / "open_env_setup.sh"


class EnvSetupTest(unittest.TestCase):
    def test_creates_private_local_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env = os.environ | {
                "VIDEO_TRANSLATE_ENV_FILE": str(env_file),
                "VIDEO_TRANSLATE_OPEN_EDITOR": "0",
            }
            result = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            contents = env_file.read_text(encoding="utf-8")
            self.assertIn("DASHSCOPE_API_KEY=", contents)
            self.assertIn("ALIYUN_WORKSPACE_ID=", contents)
            self.assertIn("OKFILE_TOKEN=", contents)
            self.assertEqual(env_file.stat().st_mode & 0o077, 0)


if __name__ == "__main__":
    unittest.main()

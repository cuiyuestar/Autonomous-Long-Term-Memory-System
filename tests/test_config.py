from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from altm.config import HighRiskFlags  # noqa: E402


class HighRiskFlagsTest(unittest.TestCase):
    def test_loads_high_risk_flags_from_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "high_risk_flags.env"
            path.write_text(
                "\n".join(
                    [
                        "# 中文注释不影响解析",
                        "export ALTM_ENABLE_DEFAULT_ACTIVE_WINDOW_IN_BUILD_CONTEXT=false",
                        "export ALTM_ENABLE_ACTIVE_WINDOW_LIFECYCLE_FEEDBACK=true",
                    ]
                ),
                encoding="utf-8",
            )

            flags = HighRiskFlags.load(path=path, environ={})

            self.assertFalse(flags.enable_default_active_window_in_build_context)
            self.assertTrue(flags.enable_active_window_lifecycle_feedback)

    def test_environment_overrides_file_and_master_switch_gates_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "high_risk_flags.env"
            path.write_text(
                "\n".join(
                    [
                        "export ALTM_ENABLE_HIGH_RISK_DEFAULTS=true",
                        "export ALTM_ENABLE_L4_PERSONA_ACTIVE_WINDOW=true",
                    ]
                ),
                encoding="utf-8",
            )

            flags = HighRiskFlags.load(
                path=path,
                environ={
                    "ALTM_ENABLE_HIGH_RISK_DEFAULTS": "false",
                    "ALTM_ENABLE_L4_PERSONA_ACTIVE_WINDOW": "true",
                },
            )

            self.assertFalse(flags.enable_high_risk_defaults)
            self.assertFalse(flags.enable_l4_persona_active_window)


if __name__ == "__main__":
    unittest.main()

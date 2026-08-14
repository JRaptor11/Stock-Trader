import unittest

from config.service_mode import ServiceMode, service_mode, validate_service_startup


class ServiceModeTests(unittest.TestCase):
    def test_missing_mode_fails_closed(self):
        with self.assertRaises(RuntimeError):
            service_mode({})

    def test_trading_requires_explicit_execution_enablement(self):
        valid = {
            "SERVICE_MODE": "paper_trading",
            "BROKER_EXECUTION_ENABLED": "true",
        }
        self.assertEqual(
            validate_service_startup(ServiceMode.PAPER_TRADING, valid),
            ServiceMode.PAPER_TRADING,
        )
        with self.assertRaises(RuntimeError):
            validate_service_startup(
                ServiceMode.PAPER_TRADING,
                {**valid, "BROKER_EXECUTION_ENABLED": "false"},
            )

    def test_research_rejects_execution_and_wrong_entry_point(self):
        valid = {
            "SERVICE_MODE": "historical_research",
            "BROKER_EXECUTION_ENABLED": "false",
        }
        self.assertEqual(
            validate_service_startup(ServiceMode.HISTORICAL_RESEARCH, valid),
            ServiceMode.HISTORICAL_RESEARCH,
        )
        with self.assertRaises(RuntimeError):
            validate_service_startup(
                ServiceMode.HISTORICAL_RESEARCH,
                {**valid, "BROKER_EXECUTION_ENABLED": "true"},
            )
        with self.assertRaises(RuntimeError):
            validate_service_startup(ServiceMode.PAPER_TRADING, valid)


if __name__ == "__main__":
    unittest.main()

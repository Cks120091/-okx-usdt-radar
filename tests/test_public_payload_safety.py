import unittest

from radar.public_payload import public_candidate_payload


class PublicPayloadSafetyTests(unittest.TestCase):
    def test_publication_execution_quote_and_freshness_are_visible(self):
        item = {
            "inst_id": "AAA-USDT-SWAP",
            "direction": "LONG",
            "market_metrics": {
                "last_price": 100.0,
                "publication_bid_price": 99.99,
                "publication_ask_price": 100.01,
                "entry_execution_price": 100.01,
                "entry_execution_price_source": "ASK",
                "ticker_sampled_at": 1_700_000_000_000,
                "ticker_age_at_publish_ms": 250,
                "ticker_refresh_status": "REFRESHED",
                # Internal comparison timestamp must stay private.
                "scan_start_ticker_ts": 1_699_999_999_000,
            },
            "data_quality": {
                "publication_ticker_status": "AVAILABLE",
                "publication_ticker_ts": 1_700_000_000_000,
                "publication_ticker_age_ms": 250,
            },
            "entry_eligibility": {
                "status": "ENTRY_READY",
                "current_price_source": "ASK",
                "publication_last_price": 100.0,
            },
        }

        payload = public_candidate_payload(item, signal=True)

        self.assertEqual(payload["market_metrics"]["entry_execution_price"], 100.01)
        self.assertEqual(
            payload["market_metrics"]["entry_execution_price_source"], "ASK"
        )
        self.assertEqual(payload["market_metrics"]["ticker_refresh_status"], "REFRESHED")
        self.assertNotIn("scan_start_ticker_ts", payload["market_metrics"])
        self.assertEqual(payload["data_quality"]["publication_ticker_status"], "AVAILABLE")
        self.assertEqual(payload["entry_eligibility"]["current_price_source"], "ASK")

    def test_binding_entry_permission_is_exposed_defensively(self):
        item = {
            "inst_id": "AAA-USDT-SWAP",
            "direction": "LONG",
            "actionable": False,
            "entry_eligibility": {
                "status": "ENTRY_READY",
                "actionable": False,
                "new_entry_allowed": False,
                "hard_blockers": ["spread"],
            },
        }

        payload = public_candidate_payload(item, signal=True)

        self.assertFalse(payload["actionable"])
        self.assertFalse(payload["entry_eligibility"]["actionable"])
        self.assertFalse(payload["entry_eligibility"]["new_entry_allowed"])
        self.assertEqual(payload["entry_eligibility"]["hard_blockers"], ["spread"])

    def test_opposite_trigger_suspension_is_visible_to_the_browser(self):
        item = {
            "inst_id": "AAA-USDT-SWAP",
            "direction": "LONG",
            "market_story": {
                "trigger": {
                    "type": "BREAKOUT",
                    "direction": "LONG",
                    "event_age_bars": 2,
                    "opposite_warning_only": True,
                    "active_episode_preserved": True,
                    "new_entry_suspended": True,
                    "new_entry_suspension_reason": (
                        "偵測到正式反向價格訊號；原計畫只保留供既有持倉管理，"
                        "暫停原方向新進場"
                    ),
                    "opposite_candidate": {
                        "direction": "SHORT",
                        "type": "REVERSAL",
                        "stage": "CONFIRMED",
                        "confirmation_level": "FULL",
                        "event_age_bars": 0,
                        # Internal details must still stay private.
                        "supporting": ["internal evidence"],
                    },
                }
            },
        }

        payload = public_candidate_payload(item, signal=True)
        trigger = payload["market_story"]["trigger"]

        self.assertTrue(trigger["new_entry_suspended"])
        self.assertIn("暫停原方向新進場", trigger["new_entry_suspension_reason"])
        self.assertEqual(trigger["opposite_candidate"]["direction"], "SHORT")
        self.assertEqual(trigger["opposite_candidate"]["type"], "REVERSAL")
        self.assertNotIn("supporting", trigger["opposite_candidate"])


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Tests for trace_stitch engine."""
from __future__ import annotations

import unittest

from identity import UeIdentity, extract_identity, pick_canonical_key
from stitch_engine import StitchEngine, flow_rank, parse_ts


class TestIdentity(unittest.TestCase):
    def test_rrc_random_value(self):
        rec = {
            "message_name": "RRC_RRC_CONNECTION_REQUEST",
            "decoding_metadata": {"decoded_message_choice": "rrcConnectionRequest"},
            "decoded": {
                "message": [
                    "c1",
                    [
                        "rrcConnectionRequest",
                        {
                            "criticalExtensions": [
                                "rrcConnectionRequest-r8",
                                {
                                    "ue-Identity": ["randomValue", ["abc123", 40]],
                                    "establishmentCause": "mo-Signalling",
                                },
                            ]
                        },
                    ],
                ]
            },
        }
        ident = extract_identity(rec)
        self.assertEqual(ident.random_value, "abc123")
        self.assertEqual(ident.ue_key, "random:abc123")

    def test_canonical_prefers_imsi(self):
        a = extract_identity({"decoded": {}, "m_tmsi": 99})
        b = UeIdentity(ue_key="imsi:302211234567890", key_type="imsi", imsi="302211234567890")
        merged = pick_canonical_key([a, b])
        self.assertTrue(merged.ue_key.startswith("imsi:"))


class TestFlowRank(unittest.TestCase):
    def test_attach_order(self):
        self.assertLess(flow_rank("rrcConnectionRequest")[0], flow_rank("rrcConnectionSetupComplete")[0])
        self.assertLess(flow_rank("S1_INITIAL_UE_MESSAGE")[0], flow_rank("S1_INITIAL_CONTEXT_SETUP_REQUEST")[0])


class TestLiveTrace(unittest.TestCase):
    @unittest.skipUnless(
        __import__("pathlib").Path(__file__).resolve().parents[2].joinpath("22_decoded").is_dir(),
        "22_decoded not present",
    )
    def test_build_index(self):
        engine = StitchEngine(max_files=2)
        rows = engine.list_ues()
        self.assertGreater(len(rows), 0)
        self.assertGreater(rows[0]["records"], 0)


if __name__ == "__main__":
    unittest.main()

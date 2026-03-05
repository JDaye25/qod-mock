import os
import unittest
from backend.main import _parse_signing_keys, _active_kid_and_key, hmac_signature_hex

class TestKeyRotation(unittest.TestCase):
    def setUp(self):
        # preserve env
        self._old = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._old)

    def test_parse_multi_keys(self):
        os.environ["QOD_SIGNING_KEYS"] = "kid1:aaa;kid2:bbb"
        keys = _parse_signing_keys()
        self.assertIn("kid1", keys)
        self.assertIn("kid2", keys)

    def test_active_kid_selection(self):
        os.environ["QOD_SIGNING_KEYS"] = "kid1:aaa;kid2:bbb"
        os.environ["QOD_ACTIVE_SIGNING_KID"] = "kid2"
        kid, key = _active_kid_and_key()
        self.assertEqual(kid, "kid2")
        self.assertIsNotNone(key)

    def test_signature_uses_requested_kid(self):
        os.environ["QOD_SIGNING_KEYS"] = "kid1:aaa;kid2:bbb"
        sig1 = hmac_signature_hex("hello", kid="kid1")
        sig2 = hmac_signature_hex("hello", kid="kid2")
        self.assertNotEqual(sig1, sig2)

if __name__ == "__main__":
    unittest.main()
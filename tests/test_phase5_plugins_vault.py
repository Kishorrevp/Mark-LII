"""Phase 5 — plugin catalog / capability search / runtime install / capability
permissions, and the DPAPI secrets vault.

Verification level: GENUINE EXECUTION for the catalog, install, loader, tool
registration, permission grants and the vault's read/write/verify logic.
DPAPI itself is Windows-only, so here it is replaced by a reversible fake
cipher; the real DPAPI round trip is verified live on the Windows machine with
a dummy secret (see CHANGELOG).
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import plugin_market as PM, secret_store                      # noqa: E402
from core.jarvis_core import register_plugin_record                     # noqa: E402
from core.permissions import Decision, PermissionEngine, PermissionPolicy  # noqa: E402
from core.plugin_loader import PluginRegistry                           # noqa: E402
from core.tools.builtin import build_default_registry                   # noqa: E402
from core.tools.registry import ToolContext                              # noqa: E402


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        secret_store.set_base_dir(self.tmp)
        (self.tmp / "config").mkdir()
        self._cat = PM.catalog_dir

    def tearDown(self):
        PM.catalog_dir = self._cat
        secret_store.set_base_dir(None)
        secret_store.set_vault_backend()
        shutil.rmtree(self.tmp, ignore_errors=True)


class CatalogTests(_Tmp):
    def test_catalog_is_pinned_and_read_without_executing(self):
        names = {c["name"]: c for c in PM.catalog()}
        self.assertEqual(set(names), {"calculator", "unit_converter", "currency_converter"})
        self.assertTrue(all(c["pinned"] for c in names.values()))
        self.assertEqual(names["currency_converter"]["permissions"], ["network"])
        evil = self.tmp / "evil.py"
        marker = self.tmp / "pwned"
        evil.write_text(f"open(r'{marker}', 'w').write('x')\nPLUGIN = {{'name': 'evil'}}\n", encoding="utf-8")
        self.assertEqual(PM.read_meta(evil), {"name": "evil"})
        self.assertFalse(marker.exists())                      # metadata read never runs the file

    def test_capability_search(self):
        m = PM.PluginMarket(tool_registry=build_default_registry())
        r = m.find("convert 100 dollars to rupees")
        self.assertEqual(r["available"][0]["name"], "currency_converter")
        self.assertEqual(m.find("how many miles is 10 km")["available"][0]["name"], "unit_converter")
        self.assertTrue(any(h["name"].startswith("web.") for h in m.find("search the web for news")["installed"]))
        self.assertEqual(m.find("fly me to the moon"), {"installed": [], "available": []})


class InstallTests(_Tmp):
    def market(self):
        reg = build_default_registry()
        plugins = PluginRegistry({}, logger=lambda m: None)
        installed = []
        return PM.PluginMarket(reg, plugins, register_plugin_record, on_installed=installed.append), reg, installed

    def test_install_loads_and_registers_without_restart(self):
        m, reg, installed = self.market()
        self.assertFalse(reg.has("unit_converter"))
        r = m.install("unit_converter")
        self.assertTrue(r.ok, r.error)
        self.assertTrue((self.tmp / "plugins" / "unit_converter.py").exists())
        self.assertEqual(installed, ["unit_converter"])
        spec = reg.get("unit_converter")
        self.assertEqual(spec.source, "plugin")
        out = reg.invoke("unit_converter", {"value": 10, "from_unit": "km", "to_unit": "miles"}, ToolContext())
        self.assertIn("6.21371", out.message or str(out.data))
        self.assertTrue(m.install("unit_converter").data.get("already"))

    def test_tampered_catalog_file_is_refused(self):
        fake = self.tmp / "cat"
        shutil.copytree(self._cat(), fake)
        (fake / "calculator.py").write_bytes((fake / "calculator.py").read_bytes() + b"\r\n# tampered\r\n")
        PM.catalog_dir = lambda: fake
        m, reg, _ = self.market()
        r = m.install("calculator")
        self.assertFalse(r.ok)
        self.assertIn("checksum", r.error)
        self.assertFalse((self.tmp / "plugins" / "calculator.py").exists())

    def test_install_needs_confirmation_and_first_use_asks_for_permissions(self):
        m, reg, _ = self.market()
        pe = PermissionEngine(reg, policy=PermissionPolicy())
        d = pe.decide("plugin.install", {"name": "currency_converter"})
        self.assertEqual(d.decision, Decision.CONFIRM)
        self.assertIn("network", reg.get("plugin.install").confirm_text({"name": "currency_converter"}))
        m.install("currency_converter")
        d1 = pe.decide("currency_converter", {"amount": 5, "from_currency": "USD", "to_currency": "INR"})
        self.assertEqual(d1.decision, Decision.CONFIRM)
        self.assertEqual(d1.grant, ["network"])
        pe.record_grant("currency_converter", d1.grant)
        d2 = pe.decide("currency_converter", {"amount": 5, "from_currency": "USD", "to_currency": "INR"})
        self.assertEqual(d2.decision, Decision.ALLOW)
        cfg = json.loads((self.tmp / "config" / "api_keys.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["plugin_permissions"], {"currency_converter": ["network"]})
        # a fresh engine reading config also remembers it
        self.assertEqual(PermissionEngine(reg).decide("currency_converter",
                                                      {"amount": 1, "from_currency": "USD",
                                                       "to_currency": "EUR"}).decision, Decision.ALLOW)

    def test_calculator_plugin_is_safe(self):
        m, reg, _ = self.market()
        m.install("calculator")
        calc = sys.modules["plugins.calculator"].calculate
        self.assertEqual(calc("2**10"), 1024)
        self.assertEqual(calc("18% of 2500"), 450)
        self.assertEqual(calc("sqrt(144)+factorial(5)"), 132)
        for bad in ("__import__('os').system('echo hi')", "open('x')", "().__class__", "9**9**9"):
            with self.assertRaises(Exception):
                calc(bad)


class VaultTests(_Tmp):
    def fake(self):
        secret_store.set_vault_backend(protect=lambda b: b"V1" + b[::-1], unprotect=lambda b: b[2:][::-1])

    def test_save_encrypts_and_reads_back(self):
        self.fake()
        secret_store.save_secret("GOOGLE_API_KEY", "AIzaSyDUMMYDUMMYDUMMYDUMMY12345")
        raw = (self.tmp / ".env").read_text(encoding="utf-8")
        self.assertIn("GOOGLE_API_KEY=dpapi:", raw)
        self.assertNotIn("AIzaSyDUMMY", raw)
        self.assertEqual(secret_store.get_secret("GOOGLE_API_KEY"), "AIzaSyDUMMYDUMMYDUMMYDUMMY12345")
        self.assertIn("encrypted", secret_store.describe()["GOOGLE_API_KEY"])

    def test_migrate_plain_env_and_failure_modes(self):
        (self.tmp / ".env").write_bytes(b"# c\r\nGOOGLE_API_KEY=AQ.Ab8RDUMMYDUMMYDUMMYDUMMYDUMMY\r\nOTHER=1\r\n")
        self.fake()
        msg = secret_store.encrypt_env_secrets()
        self.assertIn("encrypted 1", msg)
        self.assertNotIn("AQ.Ab8", msg)
        raw = (self.tmp / ".env").read_text(encoding="utf-8")
        self.assertIn("OTHER=1", raw)
        self.assertNotIn("AQ.Ab8RDUMMY", raw)
        self.assertEqual(secret_store.get_secret("GOOGLE_API_KEY"), "AQ.Ab8RDUMMYDUMMYDUMMYDUMMYDUMMY")
        self.assertIn("nothing", secret_store.encrypt_env_secrets())          # idempotent
        # a different Windows account (unprotect fails) → empty, never garbage
        secret_store.set_vault_backend(protect=lambda b: b, unprotect=lambda b: (_ for _ in ()).throw(OSError()))
        self.assertEqual(secret_store.get_secret("GOOGLE_API_KEY"), "")
        self.assertIn("cannot be decrypted", secret_store.describe()["GOOGLE_API_KEY"])

    def test_bad_cipher_never_loses_the_key(self):
        secret_store.set_vault_backend(protect=lambda b: b"garbage", unprotect=lambda b: b"nope")
        secret_store.save_secret("GOOGLE_API_KEY", "AIzaSyKEEPKEEPKEEPKEEPKEEP00000")
        self.assertEqual(secret_store.get_secret("GOOGLE_API_KEY"), "AIzaSyKEEPKEEPKEEPKEEPKEEP00000")

    def test_redaction_covers_new_formats(self):
        s = secret_store.redact("key AQ.Ab8RN6LfDUMMYDUMMYDUMMYDUMMYxx and dpapi:QUJDREVGR0hJSktMTU5PUA==")
        self.assertNotIn("AQ.Ab8RN6", s)
        self.assertNotIn("QUJDREVGR0hJSktMTU5PUA", s)

    def test_not_windows_means_plain(self):
        if sys.platform.startswith("win"):
            self.skipTest("real DPAPI available")
        self.assertFalse(secret_store.vault_available())
        self.assertIn("not available", secret_store.encrypt_env_secrets())


if __name__ == "__main__":
    unittest.main()

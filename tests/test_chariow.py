"""Tests SIMULÉS du récepteur de Pulses Chariow (aucun appel réseau, aucun vrai secret)."""

import json
import unittest
from unittest import mock

import chariow
import store as store_module
import test_charyam_telegram as base

bot = base.bot
SECRET = "whsec_secret_de_test_123"


def body(event="successful.sale", sale_id="sal_1", amount=2000, status="completed", extra=None):
    d = {
        "event": event,
        "sale": {"id": sale_id, "amount": {"value": amount, "currency": "XOF"}, "status": status},
        "product": {"id": "prd_1", "name": "Guide CV https://x.io/é"},
        "customer": {"id": "cus_1", "email": "client@example.com"},
    }
    if extra:
        d.update(extra)
    return json.dumps(d, separators=(",", ":")).replace("/", "\\/").encode("ascii")  # comme Chariow : \/ et \uXXXX


def headers(raw, event="successful.sale", delivery="del_1", secret=SECRET, sig=None):
    return {
        "X-Chariow-Signature": sig if sig is not None else chariow.compute_signature(raw, secret),
        "X-Pulse-Delivery-Id": delivery,
        "X-Pulse-Event": event,
    }


class SignatureTest(unittest.TestCase):
    def test_signature_valide_et_corps_modifie(self):
        raw = body()
        self.assertTrue(chariow.verify_signature(raw, chariow.compute_signature(raw, SECRET), SECRET))
        self.assertFalse(chariow.verify_signature(raw + b" ", chariow.compute_signature(raw, SECRET), SECRET))

    def test_refus(self):
        raw = body()
        good = chariow.compute_signature(raw, SECRET)
        for received, secret in (("", SECRET), (good[7:], SECRET), (good, ""), (good, "autre"), ("md5=" + good[7:], SECRET)):
            self.assertFalse(chariow.verify_signature(raw, received, secret))

    def test_vecteur_independant(self):
        import hashlib, hmac
        expected = "sha256=" + hmac.new(b"k", b"{}", hashlib.sha256).hexdigest()
        self.assertEqual(chariow.compute_signature(b"{}", "k"), expected)


class RouteTest(unittest.TestCase):
    def setUp(self):
        self.client = bot.app.test_client()
        self.fake = store_module.FakeStore()
        self.sent = []
        self.patches = [
            mock.patch.object(bot, "CHARIOW_PULSE_SECRET", SECRET),
            mock.patch.object(bot, "persistent_store", self.fake),
            mock.patch.object(bot, "send_telegram_message", lambda chat, text, **k: self.sent.append((chat, text))),
            mock.patch.object(bot, "executor", mock.Mock(submit=lambda f, *a: f(*a))),
        ]
        for p in self.patches:
            p.start()
        bot._chariow_seen.clear()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def post(self, raw, **kw):
        return self.client.post("/webhooks/chariow", data=raw, headers=headers(raw, **kw), content_type="application/json")

    def test_vente_payee_enregistree_et_proprietaire_prevenu(self):
        r = self.post(body())
        self.assertEqual(r.status_code, 200)
        order = self.fake.json_data["charyam:order:sal_1"]
        self.assertEqual((order["status"], order["amount"], order["currency"]), ("paid", 2000, "XOF"))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], bot.USER_ID)
        self.assertIn("Vente payée", self.sent[0][1])

    def test_retry_meme_livraison_une_seule_fois(self):
        raw = body()
        self.post(raw); self.post(raw)
        self.assertEqual(len(self.sent), 1)

    def test_signature_invalide_401_sans_effet(self):
        raw = body()
        r = self.client.post("/webhooks/chariow", data=raw, headers=headers(raw, sig="sha256=" + "0" * 64))
        self.assertEqual(r.status_code, 401)
        self.assertFalse(self.fake.json_data); self.assertFalse(self.sent)

    def test_sans_secret_503(self):
        with mock.patch.object(bot, "CHARIOW_PULSE_SECRET", ""):
            raw = body()
            self.assertEqual(self.post(raw).status_code, 503)
        self.assertFalse(self.fake.json_data)

    def test_echec_et_abandon_jamais_payes(self):
        self.post(body("failed.sale", "sal_f", status="failed"), event="failed.sale", delivery="d_f")
        self.post(body("abandoned.sale", "sal_a", status="abandoned"), event="abandoned.sale", delivery="d_a")
        self.assertEqual(self.fake.json_data["charyam:order:sal_f"]["status"], "failed")
        self.assertEqual(self.fake.json_data["charyam:order:sal_a"]["status"], "abandoned")
        self.assertFalse(any("Vente payée" in t for _, t in self.sent))

    def test_ordre_inverse_paye_puis_echec_reste_paye(self):
        self.post(body(), delivery="d1")
        self.post(body("failed.sale", status="failed"), event="failed.sale", delivery="d2")
        self.assertEqual(self.fake.json_data["charyam:order:sal_1"]["status"], "paid")
        self.assertEqual(len(self.sent), 1)

    def test_evenement_non_vente_ignore(self):
        raw = json.dumps({"event": "license.issued"}).encode()
        self.assertEqual(self.post(raw, event="license.issued").status_code, 200)
        self.assertFalse(self.fake.json_data); self.assertFalse(self.sent)

    def test_requete_incomplete_400(self):
        raw = b"[1]"
        self.assertEqual(self.post(raw).status_code, 400)
        raw = body()
        r = self.client.post("/webhooks/chariow", data=raw, headers={"X-Chariow-Signature": chariow.compute_signature(raw, SECRET)})
        self.assertEqual(r.status_code, 400)

    def test_test_chariow_marque_test_et_sans_montant(self):
        raw = json.dumps({"event": "successful.sale", "sale": {"id": "test_sale_9", "status": "completed"}, "note": "t"}).encode()
        self.assertEqual(self.post(raw).status_code, 200)
        self.assertIn("TEST", self.sent[0][1]); self.assertIn("montant inconnu", self.sent[0][1])

    def test_stockage_en_panne_503_puis_reprise_notifie_une_fois(self):
        raw = body()
        self.fake.down = True
        self.assertEqual(self.post(raw).status_code, 503)
        self.assertFalse(self.sent)
        self.fake.down = False
        self.assertEqual(self.post(raw).status_code, 200)
        self.assertEqual(len(self.sent), 1)

    def test_sans_stockage_dedoublonnage_local(self):
        with mock.patch.object(bot, "persistent_store", None):
            raw = body()
            self.post(raw); self.post(raw)
        self.assertEqual(len(self.sent), 1)


class UpstashClaimTest(unittest.TestCase):
    def test_claim_set_nx(self):
        import httpx
        calls = []
        def handler(req):
            calls.append(json.loads(req.content))
            return httpx.Response(200, json={"result": "OK" if len(calls) == 1 else None})
        s = store_module.UpstashStore("https://x.upstash.io", "tok", transport=httpx.MockTransport(handler))
        self.assertTrue(s.claim("k", 60)); self.assertFalse(s.claim("k", 60))
        self.assertEqual(calls[0], ["SET", "k", "1", "EX", "60", "NX"])


if __name__ == "__main__":
    unittest.main()

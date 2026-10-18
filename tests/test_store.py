"""Tests SIMULÉS de la mémoire persistante et des quotas persistants (aucun accès réseau).

Upstash est remplacé par httpx.MockTransport (forme des requêtes) ou par FakeStore (comportement).
Ces tests ne prouvent rien sur le service réel : voir docs/MEMOIRE_PERSISTANTE.md pour le test réel.
"""

import json
import unittest
from unittest import mock

import httpx

import access
import store
import test_charyam_telegram as base
from store import FakeStore, StoreError, UpstashStore

bot = base.bot
TOKEN = "jeton-upstash-test"
URL = "https://exemple.upstash.io"


def make(handler):
    return UpstashStore(URL, TOKEN, transport=httpx.MockTransport(handler))


class UpstashStoreTest(unittest.TestCase):
    def test_lecture_historique_forme_de_la_requete(self):
        seen = {}

        def handler(request):
            seen["method"] = request.method
            seen["auth"] = request.headers["authorization"]
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"result": json.dumps([{"role": "user", "content": "a"}])})

        result = make(handler).get_history(42)
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["auth"], f"Bearer {TOKEN}")
        self.assertEqual(seen["body"], ["GET", "charyam:hist:42"])
        self.assertEqual(result, [{"role": "user", "content": "a"}])

    def test_historique_absent(self):
        self.assertIsNone(make(lambda r: httpx.Response(200, json={"result": None})).get_history(1))

    def test_historique_invalide_est_filtre_ou_ignore(self):
        mixed = [{"role": "user", "content": "ok"}, {"role": "system", "content": "x"},
                 {"role": "assistant", "content": 5}, "texte", {"role": "assistant", "content": "ok2"}]
        got = make(lambda r: httpx.Response(200, json={"result": json.dumps(mixed)})).get_history(1)
        self.assertEqual([m["content"] for m in got], ["ok", "ok2"])
        self.assertIsNone(make(lambda r: httpx.Response(200, json={"result": "pas du json"})).get_history(1))
        self.assertIsNone(make(lambda r: httpx.Response(200, json={"result": json.dumps({"a": 1})})).get_history(1))

    def test_ecriture_avec_expiration_7_jours(self):
        bodies = []

        def handler(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"result": "OK"})

        make(handler).set_history(42, [{"role": "user", "content": "é"}])
        self.assertEqual(bodies[0][0:2], ["SET", "charyam:hist:42"])
        self.assertEqual(bodies[0][3:], ["EX", "604800"])
        self.assertEqual(json.loads(bodies[0][2]), [{"role": "user", "content": "é"}])

    def test_historique_trop_gros_refuse_sans_appel_reseau(self):
        calls = []
        big = [{"role": "user", "content": "x" * (store.MAX_STORED_HISTORY_BYTES + 1)}]
        with self.assertRaises(StoreError):
            make(lambda r: calls.append(1) or httpx.Response(200, json={"result": "OK"})).set_history(1, big)
        self.assertEqual(calls, [])

    def test_suppression(self):
        bodies = []
        make(lambda r: bodies.append(json.loads(r.content)) or httpx.Response(200, json={"result": 1})).delete_history(42)
        self.assertEqual(bodies, [["DEL", "charyam:hist:42"]])

    def test_erreurs_ne_revelent_pas_le_jeton(self):
        def boom(request):
            raise httpx.ConnectError(f"échec avec {TOKEN}", request=request)

        cases = [
            lambda r: httpx.Response(500, text=TOKEN),
            lambda r: httpx.Response(200, text="pas json"),
            lambda r: httpx.Response(200, json={"error": f"ERR {TOKEN}"}),
            boom,
        ]
        for handler in cases:
            with self.assertRaises(StoreError) as ctx:
                make(handler).get_history(1)
            self.assertNotIn(TOKEN, str(ctx.exception))

    def test_compteur_expiration_fixee_seulement_a_la_creation(self):
        commands, values = [], iter([1, 2])

        def handler(request):
            body = json.loads(request.content)
            commands.append(body)
            return httpx.Response(200, json={"result": next(values) if body[0] == "INCR" else 1})

        s = make(handler)
        self.assertEqual(s.incr_with_ttl("k", 120), 1)
        self.assertEqual(s.incr_with_ttl("k", 120), 2)
        self.assertEqual([c[0] for c in commands], ["INCR", "EXPIRE", "INCR"])
        self.assertEqual(commands[1], ["EXPIRE", "k", "120"])

    def test_compteur_invalide(self):
        with self.assertRaises(StoreError):
            make(lambda r: httpx.Response(200, json={"result": "abc"})).incr_with_ttl("k", 60)

    def test_url_non_https_ou_jeton_vide_refuses(self):
        with self.assertRaises(ValueError):
            UpstashStore("http://exemple.upstash.io", TOKEN)
        with self.assertRaises(ValueError):
            UpstashStore(URL, "  ")

    def test_make_store(self):
        self.assertIsNone(store.make_store({}))
        self.assertIsNone(store.make_store({"UPSTASH_REDIS_REST_URL": URL}))                  # jeton manquant
        self.assertIsNone(store.make_store({"UPSTASH_REDIS_REST_URL": "http://x", "UPSTASH_REDIS_REST_TOKEN": "t"}))
        self.assertIsInstance(
            store.make_store({"UPSTASH_REDIS_REST_URL": URL, "UPSTASH_REDIS_REST_TOKEN": TOKEN}), UpstashStore
        )


class PersistentQuotaTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeStore()
        self.ctl = access.AccessControl(
            1, env={"USER_MESSAGES_PER_MINUTE": "2", "USER_MESSAGES_PER_DAY": "3", "ALLOWED_USER_IDS": "9"},
            store=self.fake,
        )

    def test_limite_par_minute_puis_nouvelle_fenetre(self):
        t = 1_800_000_000
        self.assertEqual(self.ctl.check_rate(9, now=t), (True, ""))
        self.assertEqual(self.ctl.check_rate(9, now=t + 1), (True, ""))
        self.assertEqual(self.ctl.check_rate(9, now=t + 2), (False, "minute"))
        self.assertEqual(self.ctl.check_rate(9, now=t + 61), (True, ""))

    def test_refus_a_la_minute_ne_consomme_pas_le_quota_du_jour(self):
        t = 1_800_000_000
        for offset in (0, 1, 2, 3):
            self.ctl.check_rate(9, now=t + offset)
        day_keys = [k for k in self.fake.counters if ":day:" in k]
        self.assertEqual([self.fake.counters[k] for k in day_keys], [2])

    def test_limite_par_jour(self):
        t = 1_800_000_000 - (1_800_000_000 % 86400) + 3600          # début de journée UTC + 1 h
        self.assertTrue(self.ctl.check_rate(9, now=t)[0])
        self.assertTrue(self.ctl.check_rate(9, now=t + 1)[0])
        self.assertTrue(self.ctl.check_rate(9, now=t + 120)[0])
        self.assertEqual(self.ctl.check_rate(9, now=t + 121), (False, "jour"))

    def test_expirations_des_compteurs(self):
        self.ctl.check_rate(9, now=1_800_000_000)
        ttls = {("min" if ":min:" in k else "day"): v for k, v in self.fake.ttls.items()}
        self.assertEqual(ttls, {"min": 120, "day": 90000})

    def test_proprietaire_exempte_sans_toucher_au_stockage(self):
        self.fake.down = True
        for _ in range(10):
            self.assertEqual(self.ctl.check_rate(1), (True, ""))
        self.assertEqual(self.fake.counters, {})

    def test_stockage_indisponible_refuse_les_testeurs(self):
        self.fake.down = True
        self.assertEqual(self.ctl.check_rate(9), (False, "indisponible"))
        self.assertIn("momentanément indisponible", access.rate_limit_text("indisponible"))

    def test_sans_stockage_comportement_inchange(self):
        local = access.AccessControl(1, env={"USER_MESSAGES_PER_MINUTE": "1", "ALLOWED_USER_IDS": "9"})
        self.assertEqual(local.check_rate(9, now=1000), (True, ""))
        self.assertEqual(local.check_rate(9, now=1001), (False, "minute"))


class BotPersistentMemoryTest(unittest.TestCase):
    def setUp(self):
        base.TELEGRAM_CALLS.clear()
        base.CLAUDE_CALLS.clear()
        base.CLAUDE_REPLY.update({"text": "Réponse de test", "stop_reason": "end_turn"})
        bot.chat_memory.clear()
        bot.chat_modes.clear()
        bot.last_answers.clear()
        self.fake = FakeStore()
        patcher = mock.patch.object(bot, "persistent_store", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = bot.app.test_client()

    def post(self, text, **kw):
        return self.client.post("/webhook", json=base._update(text, **kw))

    def sent_texts(self):
        return [p["text"] for m, p in base.TELEGRAM_CALLS if m == "sendMessage"]

    def test_conversation_sauvegardee_puis_rechargee_apres_redemarrage(self):
        self.post("bonjour")
        saved = self.fake.histories[store.history_key(42)]
        self.assertEqual([m["role"] for m in saved], ["user", "assistant"])
        bot.chat_memory.clear()                                   # simule un redémarrage du service
        self.post("la suite")
        contents = [m["content"] for m in base.CLAUDE_CALLS[-1]["messages"]]
        self.assertEqual(contents, ["bonjour", "Réponse de test", "la suite"])

    def test_panne_du_stockage_ne_coupe_pas_la_conversation(self):
        self.fake.down = True
        self.post("bonjour")
        self.assertEqual(self.sent_texts()[-1], "Réponse de test")
        self.assertEqual(len(bot.chat_memory[42]), 2)             # copie locale conservée
        self.post("encore")                                       # la suite fonctionne aussi
        self.assertEqual(self.sent_texts()[-1], "Réponse de test")

    def test_panne_au_rechargement_repart_d_une_conversation_vide(self):
        self.post("bonjour")
        bot.chat_memory.clear()
        self.fake.down = True
        self.post("la suite")
        self.assertEqual([m["content"] for m in base.CLAUDE_CALLS[-1]["messages"]], ["la suite"])

    def test_oubli_efface_local_et_persistant(self):
        self.post("bonjour")
        self.post("/oubli")
        self.assertEqual(self.fake.histories, {})
        self.assertNotIn(42, bot.chat_memory)
        self.assertNotIn(42, bot.last_answers)
        self.assertEqual(self.sent_texts()[-1], bot.FORGET_DONE_TEXT)
        self.post("nouveau départ")
        self.assertEqual([m["content"] for m in base.CLAUDE_CALLS[-1]["messages"]], ["nouveau départ"])

    def test_oubli_avec_stockage_en_panne_le_dit_honnetement(self):
        self.post("bonjour")
        self.fake.down = True
        self.post("/oubli")
        self.assertEqual(self.sent_texts()[-1], bot.FORGET_PARTIAL_TEXT)
        self.assertNotIn(42, bot.chat_memory)

    def test_oubli_sans_stockage_persistant(self):
        with mock.patch.object(bot, "persistent_store", None):
            self.post("bonjour")
            self.post("/oubli")
            self.assertEqual(self.sent_texts()[-1], bot.FORGET_DONE_TEXT)
            self.assertNotIn(42, bot.chat_memory)

    def test_testeur_refuse_quand_le_stockage_des_quotas_est_en_panne(self):
        self.fake.down = True
        with mock.patch.object(bot.access_control, "store", self.fake), \
                mock.patch.object(bot.access_control, "allowed", frozenset({bot.USER_ID, 777})):
            self.post("bonjour", user_id=777, chat_id=777)
        self.assertEqual(base.CLAUDE_CALLS, [])                    # aucun appel à Claude
        self.assertIn("momentanément indisponible", self.sent_texts()[-1])

    def test_le_jeton_upstash_est_masque_dans_les_journaux(self):
        with mock.patch.object(bot, "UPSTASH_TOKEN", "jeton-secret-upstash"):
            self.assertEqual(bot.redact("erreur jeton-secret-upstash fin"), "erreur *** fin")


if __name__ == "__main__":
    unittest.main()

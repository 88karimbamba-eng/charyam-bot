"""Tests SIMULÉS : le webhook répond tout de suite à Telegram, même si les quotas (stockage) sont lents ou bloqués.

Cas réel observé : un stockage qui ne répond pas faisait expirer la requête de Telegram ; Telegram renvoyait le
message, le bot l'écartait comme doublon et le client restait sans réponse.
"""

import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

import test_charyam_telegram as base

bot = base.bot

CLIENT = 888000333


def wait_for(condition, seconds=3.0):
    end = time.time() + seconds
    while time.time() < end:
        if condition():
            return True
        time.sleep(0.01)
    return False


class WebhookImmediateTest(unittest.TestCase):
    def setUp(self):
        base.TELEGRAM_CALLS.clear()
        base.CLAUDE_CALLS.clear()
        bot.chat_memory.clear()
        bot.chat_modes.clear()
        self.client = bot.app.test_client()
        self.release = threading.Event()
        self.old_executor = bot.executor
        self.old_check = bot.access_control.check_rate
        self.old_timeout = bot.QUOTA_TIMEOUT_SECONDS
        bot.executor = ThreadPoolExecutor(max_workers=2)       # vrais fils : le webhook ne doit pas attendre

    def tearDown(self):
        self.release.set()
        bot.executor.shutdown(wait=True)
        bot.executor = self.old_executor
        bot.access_control.check_rate = self.old_check
        bot.QUOTA_TIMEOUT_SECONDS = self.old_timeout

    def block_quotas(self):
        def slow(user_id, now=None):
            self.release.wait(5)
            return True, ""
        bot.access_control.check_rate = slow

    def texts(self):
        return [p.get("text", "") for m, p in base.TELEGRAM_CALLS
                if m == "sendMessage" and p.get("chat_id") == CLIENT]

    def test_webhook_repond_avant_la_fin_des_quotas(self):
        self.block_quotas()
        started = time.time()
        response = self.client.post("/webhook", json=base._update("Une question", user_id=CLIENT, chat_id=CLIENT))
        self.assertEqual(response.status_code, 200)
        self.assertLess(time.time() - started, 1.0)              # Telegram reçoit sa réponse sans attendre
        self.assertEqual(base.CLAUDE_CALLS, [])                  # quotas encore en cours
        self.release.set()
        self.assertTrue(wait_for(lambda: len(base.CLAUDE_CALLS) == 1))   # puis le message est bien traité
        self.assertTrue(wait_for(lambda: len(self.texts()) == 1))

    def test_ligne_de_reception_ecrite_avant_les_quotas(self):
        self.block_quotas()
        with self.assertLogs("charyam", level="INFO") as logs:
            self.client.post("/webhook", json=base._update("Une question", user_id=CLIENT, chat_id=CLIENT))
        self.assertTrue(any("Message reçu" in line for line in logs.output))

    def test_renvoi_de_telegram_pendant_le_traitement_pas_de_double_reponse(self):
        self.block_quotas()
        update = base._update("Une question", user_id=CLIENT, chat_id=CLIENT)
        self.client.post("/webhook", json=update)
        self.client.post("/webhook", json=update)                 # Telegram renvoie le même message
        self.release.set()
        self.assertTrue(wait_for(lambda: len(base.CLAUDE_CALLS) == 1))
        time.sleep(0.2)
        self.assertEqual(len(base.CLAUDE_CALLS), 1)

    def test_stockage_bloque_le_client_recoit_une_explication(self):
        self.block_quotas()
        bot.QUOTA_TIMEOUT_SECONDS = 0.2
        self.client.post("/webhook", json=base._update("Une question", user_id=CLIENT, chat_id=CLIENT))
        self.assertTrue(wait_for(lambda: any("momentanément indisponible" in t for t in self.texts())))
        self.assertEqual(base.CLAUDE_CALLS, [])                  # message refusé proprement, aucun coût Claude

    def test_apres_un_blocage_le_service_repond_de_nouveau(self):
        self.block_quotas()
        bot.QUOTA_TIMEOUT_SECONDS = 0.2
        self.client.post("/webhook", json=base._update("Une question", user_id=CLIENT, chat_id=CLIENT))
        self.assertTrue(wait_for(lambda: len(self.texts()) == 1))
        self.release.set()
        bot.access_control.check_rate = self.old_check
        bot.QUOTA_TIMEOUT_SECONDS = self.old_timeout
        bot.access_control._notices.clear()
        self.client.post("/webhook", json=base._update("Une autre question", user_id=CLIENT, chat_id=CLIENT))
        self.assertTrue(wait_for(lambda: len(base.CLAUDE_CALLS) == 1))


if __name__ == "__main__":
    unittest.main()

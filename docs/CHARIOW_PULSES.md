# Réception des ventes Chariow (Pulses)

Route : `POST /webhooks/chariow` sur le bot Render. Code : `chariow.py`. Tests : `tests/test_chariow.py` (simulés).

## Ce que fait la route
1. Sans `CHARIOW_PULSE_SECRET` : répond 503 (aucune vente acceptée sans secret).
2. Vérifie `x-chariow-signature` (HMAC-SHA256 du corps brut, secret `whsec_...` du Pulse) : sinon 401.
3. Enregistre la commande (`charyam:order:<id vente>`), dédoublonne sur `x-pulse-delivery-id`, prévient Karim sur Telegram.
4. Stockage indisponible : 503, Chariow réessaiera (5 tentatives sur ~3 h, puis le Pulse est désactivé).
5. `successful.sale` seul donne le statut « paid ». Un échec ou abandon ne remplace jamais une vente payée.

## Ce qu'elle ne fait PAS encore
- Aucun e-mail client ni livraison automatique. Chariow livre déjà le produit lui-même ; l'e-mail CHARYAM viendra après choix du service (Resend/Brevo).
- Pas de recontrôle auprès de l'API Chariow (le bot n'a pas de clé API Chariow) ni de comparaison avec le prix attendu.
- Sans Upstash, le dédoublonnage est en mémoire locale : perdu au redémarrage, la commande n'est pas conservée.

## Activation (Karim, rien n'est actif tant que ce n'est pas fait)
1. Chariow > Automations > Pulses : créer un Pulse vers `https://charyam-bot.onrender.com/webhooks/chariow`, événements successful/failed/abandoned sale.
2. Copier son « Signing secret » (`whsec_...`) UNIQUEMENT dans Render > charyam-bot > Environment, variable `CHARIOW_PULSE_SECRET`.
3. « Send test pulse » : Karim reçoit sur Telegram « Vente payée (TEST Chariow) ». Vérifier dans l'onglet Deliveries : statut 200.
4. Les 2 Pulses actuels vers Make sont obsolètes : à désactiver après réussite du test.

## Points non vérifiés
- Format exact des payloads réels (seule la doc a servi) : rejouer une livraison réelle après la première vente.
- Statut final d'un test Chariow : la doc dit que le test est un `successful.sale` avec `sale.id` préfixé `test_`.

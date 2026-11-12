# kia-prospector

Prospection quotidienne pour Kreyòl IA, exécutée par GitHub Actions (gratuit).

- Sources : OpenStreetMap (entreprises haïtiennes avec téléphone/email) et Tavily + LLM (diaspora, ONG).
- Un segment par jour (rotation), 25 prospects max, envoyés à l'agent Cloudflare (`POST /api/prospects`).
- L'agent rédige les brouillons ; tu valides sur Telegram. Rien n'est envoyé aux prospects automatiquement.
- Les contacts extraits par le LLM sont vérifiés mot pour mot dans le texte source (pas d'invention).

## Secrets GitHub (Settings > Secrets and variables > Actions)
KIA_AGENT_URL (https://kia-agent.destoleika7.workers.dev), KIA_API_TOKEN, TAVILY_API_KEY (optionnel, pour diaspora/ONG), GROQ_API_KEY, GEMINI_API_KEY (optionnel).

## Test
`python -m unittest` · simulation : `python prospector.py --dry-run --segment clinics_ht`
Actions > Prospection quotidienne > Run workflow (champ dry_run pour simuler).

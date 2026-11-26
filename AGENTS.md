---
tags: [documentacao, ia, contexto]
date: 2026-11-26
---

# Orientações para IAs no Hub IA

Comece pelo [guia de contexto para IAs](docs/AI_CONTEXT_GUIDE.md) e pelo [índice da documentação](docs/00-HubIA-Home.md). No Obsidian, a entrada é [[docs/00-HubIA-Home|Hub IA — Central de documentação]].

- Localize a rota em `apps/portal/views.py` e siga a autorização, a execução e a persistência antes de editar. Para acesso a espaços, revise também as migrações de RLS em `packages/database/migrations/`.
- Preserve a identidade do agente e o histórico ao alterar seu catálogo. Valide o comportamento pelo portal e pelas integrações que usam o mesmo agente.
- Mantenha credenciais e dados de usuários fora dos exemplos, logs e diffs. Use um banco descartável e fixtures para testes de permissões e de conectores.
- Atualize as notas afetadas em `docs/` usando YAML, WikiLinks desde a raiz deste repositório, diagramas Mermaid em fluxos críticos e links do GitHub para fontes de código. Registre a revisão e a evidência de validação. O vault da Peso Exato é a referência de formato desta documentação local.

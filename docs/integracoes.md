# Inventário inicial de integrações

| Integração | Evidência disponível | Próxima validação |
| --- | --- | --- |
| WhatsIA | Existe uma cópia de trabalho ao lado do HUB. O README descreve fila local, um webhook de saída com idempotência, interface de linha de comando e rotas locais de ações do WhatsApp. | Confirmar propriedade das contas e histórico, escolher capacidades somente de leitura para o primeiro adaptador e testar autenticação e respostas no serviço autorizado. |
| Monitor GitHub Actions | A arquitetura prevê consulta somente de leitura e exclusão de repositórios pessoais. O serviço em execução e suas configurações ainda precisam ser localizados. | Identificar a automação responsável e validar deduplicação antes de adicionar outra. |

## Dot e clientes MCP

Em **Espaços → [nome do espaço] → Minhas integrações**, cada conexão pertence a um usuário e a um único espaço. Use o cadastro automático no ChatGPT para o espaço pessoal. Para trabalhar em **Peso Exato → Desenvolvimento**, abra esse espaço no HUB, crie um cliente separado em **Cadastro manual de conexão**, informe a URI de retorno exibida pelo cliente e copie as credenciais para a configuração OAuth do segundo conector. O catálogo e as tarefas dependem da conexão usada. Os demais membros do setor não recebem acesso ao Dot pessoal.

O servidor MCP atende em `https://hubia.psxt.io/conta/mcp`. A autorização requer login no HUB, consentimento e código OAuth com PKCE S256. A permissão `hub.read` permite consultar o catálogo e as tarefas do vínculo. A permissão `hub.reply` permite devolver uma resposta quando o cliente oferece escrita e o papel no espaço permite. A página de consentimento identifica o espaço autorizado.

O Dot pode chamar `list_agents`, `list_tasks` e `fetch_task`. Clientes autorizados a escrever também recebem `reply_task`. Para notificações, peça ao Dot que assine o evento `hub.task.created` na conversa desejada. O HUB verifica o endereço de entrega, assina o aviso e agenda novas tentativas em caso de falha. O aviso contém a identificação da tarefa. O texto é consultado pela conexão autenticada. Confira no portal se há assinatura ativa, notificação aceita ou resposta registrada. Clientes sem suporte a eventos podem consultar as tarefas pendentes.

## Outros agentes via A2A

O Agent Card público fica em `https://hubia.psxt.io/.well-known/agent-card.json`. Para um agente consultar o HUB, crie uma conexão do tipo **Outro agente consulta o HUB (A2A)** e configure a chave Bearer no agente. O serviço oferece catálogo e busca textual conforme as permissões do espaço.

Para o HUB consultar outro agente, escolha **HUB consulta um agente remoto (A2A)** e informe o URL HTTPS do Agent Card e, se necessário, o Bearer do serviço. A implementação usa a interface JSON-RPC A2A 1.0, mensagens de texto e consulta de andamento para agentes que devolvem uma tarefa assíncrona. A credencial fica restrita à origem do card.

## Notificações Peso Exato

O agente pertence ao espaço Principal da Peso Exato. Em **Gerenciar agentes → Configurar canais e destinos**, salve o identificador da integração de texto e a chave da Leadfinder e habilite o WhatsApp. O identificador é o segmento entre `enviarMensagem/` e `/TEXTO` no endereço fornecido pela Leadfinder. Quem envia informa o destinatário a cada aviso. Para oferecer atalhos ou restringir um destino a um setor, cadastre-o pelo nome. Os usuários precisam de acesso ao espaço que contém o agente. O papel de leitor pode consultar o catálogo.

Na página do agente, escolha o canal, informe o telefone com código do país ou o ID do grupo e escreva a mensagem. O histórico identifica o canal e o destino. Para uso via Dot, autorize uma conexão MCP no mesmo espaço com o escopo `hub.notify`. Para consultar o contrato e os exemplos sem enviar nada, chame `descrever_envio` com `{}`. A ferramenta `listar_destinos` retorna os destinos permitidos. A ferramenta `enviar_aviso` aceita `agent_id`, `canal`, `mensagem`, um UUID `request_id` e exatamente um dos campos `destinatario` (endereço direto) ou `destino` (ID de um atalho). Reutilize o mesmo UUID ao consultar o resultado da mesma solicitação, para evitar duplicação. Depois de um resultado incerto, confira o recebimento antes de solicitar outro envio.

O primeiro adaptador é WhatsApp. Para acrescentar e-mail, SMS ou Telegram, implemente o transporte em `notification_channels.py`, registre o canal em `CHANNELS` e acrescente a configuração de credenciais e a validação dos endereços do provedor. O agente, as regras por destino e o histórico de envios são compartilhados entre os canais.

## Conector institucional do GitHub

Em **Administração → Instituições → Conectores → GitHub**, informe a organização e registre a GitHub App privada. Um responsável com acesso à organização no GitHub conclui o registro e a instalação. Selecione **Only select repositories** e marque os repositórios autorizados, começando por **Obsidian**. O HUB recebe a chave do aplicativo pelo fluxo de manifesto, armazena a chave cifrada e usa tokens temporários de instalação com leitura de conteúdo. A credencial permanece no servidor.

De volta ao HUB, clique em **Testar conexão**. O HUB consulta a instalação na organização, lista os repositórios autorizados e consulta o commit da branch padrão do repositório escolhido. A página exibe o horário do teste e o link da revisão. Quando o teste falha, a página exibe o diagnóstico e remove a listagem anterior. Gerencie o acesso aos repositórios na instalação no GitHub e repita o teste após alterações.

### Biblioteca de notas por instituição

Depois de verificar o conector, escolha o repositório e o nome do agente em **Biblioteca de notas**. A criação disponibiliza um agente no catálogo da instituição e começa com zero pastas liberadas. Cada administrador pode conceder pastas nos espaços que administra e revogar o acesso salvando o campo vazio. Informe caminhos relativos, um por linha, com a mesma grafia do GitHub. Cada pasta inclui suas subpastas. Para todas as pastas atuais e futuras e as notas na raiz, salve apenas `/` no espaço desejado. Arquivos ocultos, links simbólicos e arquivos fora das pastas liberadas ficam fora da consulta.

No portal, abra o agente pelo espaço. No MCP, a conexão desse espaço passa a oferecer `buscar_notas` e `ler_nota` quando recebe sua primeira pasta. A busca percorre nomes e texto em páginas; para avançar, envie a próxima página e a revisão devolvidas pelo HUB. A leitura usa o caminho exato retornado na busca e traz o texto Markdown, a revisão e o link da fonte. O conteúdo é lido da branch padrão no momento da consulta.

### Avisos de disponibilidade do catálogo

Em **Gerenciar agentes → Avisos às integrações**, habilite os avisos para o espaço. A criação de um agente ativo, sua reativação e a primeira liberação da biblioteca nesse espaço publicam um aviso para cada conexão configurada. Editar o nome ou ampliar uma lista de pastas já liberadas preserva o histórico sem repetir o anúncio.

Em MCP, o aviso chega pelo evento assinado `hub.task.created`. O cliente chama `fetch_task` e identifica o aviso pelo tipo `hub.agent.available` e pelo campo `agent_id`. Conexões sem webhook podem consultar `list_tasks`; uma assinatura nova ou renovada recebe os avisos ainda pendentes. Em A2A, conexões de saída recebem `SendMessage` no agente remoto. Conexões apenas de entrada podem enviar “listar avisos”. O HUB registra a entrega na página **Minhas integrações** e repete tentativas temporariamente indisponíveis usando o mesmo identificador.

### Enviar e-mail pela caixa do usuário

Em **Espaços das minhas IAs**, o usuário configura **Ler minha caixa de entrada** e **Enviar e-mails em meu nome** por conexão MCP e salva as permissões. A caixa precisa estar vinculada em **Meu e-mail**, o conector institucional ativo e ao menos um espaço autorizado. O envio começa desligado e exige papel com escrita na instituição. Desconectar ou substituir a caixa remove suas autorizações.

O cliente solicita os escopos OAuth `hub.read hub.mail.send` e, se precisar renovar o token, `offline_access`. Conexões com token anterior precisam de nova autorização para o escopo adicional. Com a permissão ativa, `tools/list` inclui `enviar_email`. Os campos são `destinatario` (um endereço), `assunto`, `mensagem` (texto simples) e `request_id` (UUID). O remetente é sempre a caixa vinculada. Repetir a chamada com os mesmos campos e UUID consulta o resultado. O estado `aceito_pelo_smtp` confirma o aceite do provedor. Em caso de estado `incerto` ou `registrado`, confira a entrega antes de criar outro envio.

### Descoberta de funções e agentes

O cliente MCP consulta `tools/list` ao conectar e após receber `notifications/tools/list_changed`. O HUB anuncia `tools.listChanged` em `initialize`. O catálogo considera o token, as permissões da conexão e os espaços disponíveis. A definição de `list_agents` inclui a revisão dos agentes e a lista autorizada em `_meta["io.psxt.hubia/agents"]`. A chamada `list_agents` continua disponível.

Clientes podem manter um GET autenticado em `/conta/mcp` com `Accept: text/event-stream`. O HUB verifica as mudanças a cada 15 segundos enquanto o fluxo estiver aberto e encerra o fluxo quando a autorização expira ou é revogada. Também pode devolver o aviso antes do resultado de um `ping` ou `tools/call` bem-sucedido quando o POST aceita SSE. Clientes que aceitam apenas JSON podem consultar `tools/list` periodicamente. A tela **Espaços das minhas IAs** mostra a última consulta registrada para cada conexão. Uma nova permissão OAuth exige consentimento do usuário.

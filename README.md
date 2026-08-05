# Entity-Monitor

Integração customizada para o **Home Assistant** que monitora o status das suas
entidades, avisa quando elas ficam **indisponíveis** (`unavailable`) e dispara
um conjunto enxuto de notificações + relatório diário no horário escolhido.

## O que ela faz

- Acompanha uma lista de entidades escolhida por você.
- Dispara 3 tipos de notificação: **N1** (primeira queda), **N2** (offline
  acumulado ultrapassou o limiar) e **N3** (relatório diário).
- Permite **excluir** entidades específicas mesmo dentro de uma integração
  monitorada.
- Guarda o histórico de quedas (sobrevive a reinícios do HA): quantas vezes
  cada entidade caiu, por quanto tempo, e o estado por integração.
- Gera um **relatório** ranqueando as entidades e integrações que mais ficam
  offline.

## Instalação

1. Copie a pasta `custom_components/entity_monitor` para dentro da pasta
   `custom_components` da sua instalação do Home Assistant. O caminho final
   deve ser `config/custom_components/entity_monitor/`.
2. Reinicie o Home Assistant.

> Via HACS: adicione este repositório como um *custom repository* do tipo
> *Integration* e instale por lá.

## Como usar

1. Vá em **Configurações → Dispositivos e Serviços → Adicionar Integração**.
2. Procure por **Entity Monitor**.
3. No formulário, informe:
   - **Integrações a monitorar** — todas as entidades destas integrações
     entram no monitoramento.
   - **Só a entidade principal de cada dispositivo** *(padrão ligado)* —
     descarta sensores de diagnóstico, RSSI, bateria, etc.
   - **Entidades extras** *(opcional)* — entidades avulsas mesmo que a
     integração delas não esteja selecionada.
   - **Entidades a ignorar** *(opcional)* — entidades a excluir do
     monitoramento, mesmo que façam parte de uma integração monitorada.
     Some completamente (sem stats, sem notificação, sem aparecer no
     relatório).
   - **Confirmar queda após (segundos)** — padrão `30`.
   - **Janela de quedas simultâneas (segundos)** — padrão `20`.
   - **Serviço de notificação** *(opcional)* — `notify.*` que recebe os
     avisos.
   - **Limiar N2 (minutos)** — quando o **tempo acumulado offline** da
     integração no dia (união das quedas) cruza esse valor, dispara o N2.
     Padrão `30`. `0` desliga N2.
   - **Hora do relatório diário (0-23)** — quando o N3 dispara e o ciclo
     reinicia. Padrão `9` (09:00 local).
   - **Reset automático (dias)** — zera estatísticas cumulativas quando
     passam N dias. Padrão `30`. `0` desliga.

## O que é criado

| Entidade | Descrição |
| --- | --- |
| `binary_sensor.entity_monitor_problem` | Liga (`on`) enquanto qualquer entidade monitorada estiver indisponível. |
| `sensor.entity_monitor_total_outages` | Número total de quedas registradas. |
| `sensor.entity_monitor_total_downtime` | Tempo total offline (em minutos). |
| `sensor.entity_monitor_downtime_report` | Relatório completo nos atributos. |
| `button.entity_monitor_test_notification` | Dispara uma notificação de teste. |
| `button.entity_monitor_reset_all` | Zera tudo: estatísticas, estado e registros de outage em curso. |

## Notificações

### Ciclo diário

O ciclo é alinhado com o **horário do relatório** (padrão `09:00`). Um "dia"
vai das 09:00 de ontem às 08:59 de hoje. Nas 09:00, o N3 dispara para toda
integração que teve quedas, e os contadores/flags do dia (`N1 disparou?`,
`N2 disparou?`) zeram — o próximo `N1` só vai voltar a disparar amanhã.

### Estados por integração

| Estado | Como entra | Como sai |
| --- | --- | --- |
| `quiet` | Inicial, no início de cada ciclo diário | 1ª queda do dia → N1, vira `active_today` |
| `active_today` | N1 já disparou hoje | Próximo `report_time_hour` → volta a `quiet` |

### Título e citação de entidades (todas as notificações)

- **Título**: `[Integração] instável` — igual pros 3 tipos. O que diferencia
  é o corpo.
- **Corpo**: até 3 entidades pelo nome. Se tiver 4 ou mais afetadas, mostra
  as 3 principais + `(+N)` no final, ex: `Aparador, Lustre, Abajur (+2)`.
- **Ranking**: as 3 são as que **mais caíram** no ciclo (empates resolvidos
  alfabeticamente por `entity_id`).

### N1 — Entidade caiu (real-time)

Dispara na 1ª queda do ciclo (integração em `quiet`). **Uma vez por dia**
por integração.

- **1 entidade**: `Luz Sala caiu.`
- **≥2 entidades**: `Luz Sala, Tomada Cozinha, Luz Quarto caíram.`
- **>3 entidades**: `Luz Sala, Tomada Cozinha, Luz Quarto (+2) caíram.`

### N2 — Offline por X minutos (real-time)

Dispara quando o **tempo acumulado offline** da integração no ciclo
(**união** dos intervalos, incluindo o outage em curso) cruza o
**Limiar N2** (padrão 30 min). **Uma vez por dia** por integração — se
voltar a cair depois, você só recebe o N3 amanhã.

- **1 entidade**: `Luz Sala ficou 30 minutos offline hoje.`
- **≥2 entidades**: `Luz Sala, Tomada Cozinha, Luz Quarto ficaram 30
  minutos offline hoje.`

O ranking do N2 usa **tempo acumulado offline** por entidade (a que mais
ficou fora aparece primeiro).

### N3 — Relatório diário (`report_time_hour`)

Dispara no `report_time_hour` (padrão 09:00) para cada integração que teve
**≥1 queda** no ciclo anterior.

- **1 entidade**: `Luz Sala caiu 3 vezes e ficou 47 minutos offline nas
  últimas 24 horas.`
- **≥2 entidades**: `Luz Sala, Tomada Cozinha, Luz Quarto caíram 7 vezes e
  ficaram 2h 47min offline nas últimas 24 horas.`

A duração é a **união** de todos os intervalos offline da integração no
ciclo (overlaps contam uma vez). Formatada como `X segundos`, `X minutos`
ou `Xh Ymin`.

### Como o tempo é somado

- Cada outage é armazenado como um intervalo `(entity_id, start, end)`.
- Enquanto a entidade ainda está caída, `end = agora` (o tempo cresce em
  tempo real).
- Para o "tempo offline" da **integração**, é feita a **união** entre os
  intervalos das entidades daquela integração — se A ficou 10 min
  sobreposto com B ficando 10 min, conta 10 min (não 20).
- Isso vale tanto pro N2 (real-time) quanto pro N3 (relatório).

### Evento `entity_monitor_notification`

Cada notificação dispara o evento `entity_monitor_notification`:

| Campo | Descrição |
| --- | --- |
| `integration` | Slug (ex: `localtuya`). |
| `integration_name` | Nome amigável (ex: `Local Tuya`). |
| `kind` | `n1` / `n2` / `n3` / `test`. |
| `scope` | `entity` (1 entidade) ou `integration` (≥2). |
| `entity_ids` | Até 3 entidades citadas, ordem = ranking. |
| `entity_names` | Nomes amigáveis correspondentes. |
| `total_affected` | Nº total de entidades afetadas no ciclo. |
| `outage_count` | Nº de quedas (bursts) no ciclo (N3). |
| `threshold_seconds` | Limiar acionado (N2). |
| `duration_seconds` | União do tempo offline (N2 e N3). |
| `title`, `message` | Conteúdo final. |

## Persistência

Tudo é salvo em disco:

- Estatísticas cumulativas (entidades e integrações).
- Estado por integração (`quiet` vs `active_today`, `n1_fired`, `n2_fired`).
- Buffer do ciclo atual (bursts + intervalos de offline).
- `started_at` dos outages em curso — restart **nunca conta a partir do
  boot**, retoma do momento real da queda.

## Reset

Dois caminhos:

- **`Reset estatísticas`** — só zera contadores cumulativos. Estado do
  ciclo preservado.
- **`Reset tudo`** — zera estatísticas, estado, ciclo atual e timers de
  outages em curso. Botão `button.entity_monitor_reset_all` ou serviço
  `entity_monitor.reset_all`.

Reset automático: a cada `auto_reset_days` (padrão 30) os contadores
cumulativos são zerados. `0` desliga.

## Testar a notificação

- **Botão** `button.entity_monitor_test_notification`.
- **Serviço** `entity_monitor.test_notification`.

Em ambos, o evento `entity_monitor_notification` é disparado com
`kind: "test"` e `test: true`.

## Eventos disparados

- `entity_monitor_unavailable` — quando o `seconds_threshold` de uma
  entidade é atingido (`level=seconds`).
- `entity_monitor_recovered` — quando a entidade volta ao normal.
- `entity_monitor_notification` — em cada notificação (ver tabela).
- `entity_monitor_report` — quando o serviço de relatório roda.

## Relatório (sob demanda)

**Pelo sensor** — `sensor.entity_monitor_downtime_report` traz nos
atributos `worst_entities` e `worst_integrations`.

**Pelo serviço** — `entity_monitor.generate_report` (com *return response*
para receber inline). Retorna ranking por entidade e por integração.

## Configurações

| Chave | Default | Descrição |
| --- | --- | --- |
| `integrations` | — | Integrações monitoradas |
| `only_primary_entity` | `true` | Só a entidade principal de cada device |
| `entities` | — | Entidades avulsas extras |
| `excluded_entities` | — | Entidades a ignorar por completo |
| `seconds_threshold` | `30` | Segundos pra confirmar uma queda |
| `coalesce_seconds` | `20` | Janela de quedas simultâneas |
| `n3_minutes_threshold` | `30` | Limiar N2 de offline acumulado (0 desliga N2) |
| `report_time_hour` | `9` | Hora local do relatório diário (0-23) |
| `notify_service` | — | Serviço `notify.*` |
| `auto_reset_days` | `30` | Reset auto de estatísticas cumulativas (`0` desliga) |

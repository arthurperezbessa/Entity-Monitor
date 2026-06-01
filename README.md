# Entity-Monitor

Integração customizada para o **Home Assistant** que monitora o status das suas
entidades, avisa quando elas ficam **indisponíveis** (`unavailable`) e dispara
um conjunto enxuto de notificações + relatório diário no horário escolhido.

## O que ela faz

- Acompanha uma lista de entidades escolhida por você.
- Dispara notificações em 3 níveis (N1, N2, N3) — ver
  [Notificações](#notificações).
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
     monitoramento, mesmo que façam parte de uma integração monitorada. Some
     completamente (sem stats, sem notificação, sem aparecer no relatório).
   - **Confirmar queda após (segundos)** — padrão `30`.
   - **Janela de quedas simultâneas (segundos)** — padrão `20`.
   - **Serviço de notificação** *(opcional)* — `notify.*` que recebe os
     avisos.
   - **Janela do N1.2 (minutos)** — janela em que a 2ª queda dispara o N1.2.
     Padrão `30`.
   - **Limiar de offline N3 (minutos)** — uma entidade nesse tempo contínuo
     offline dispara o N3.1. Também é o limiar pra inclusão no relatório
     diário N3.2. Padrão `30`.
   - **Hora do relatório diário (0-23)** — quando os relatórios N2/N3.2
     disparam. Padrão `7` (07:00 local).
   - **Reset automático (dias)** — zera estatísticas E reseta o estado
     `silent → quiet` quando passam N dias sem qualquer queda na integração.
     Padrão `30`. `0` desliga ambos.

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

### Estados por integração

Cada integração caminha por 3 estados:

| Estado | Como entra | Como sai |
| --- | --- | --- |
| `quiet` | Inicial, ou após 30 dias sem nenhuma queda | 1ª queda → N1.1, vira `active_day1` |
| `active_day1` | N1.1 acabou de disparar | No próximo `report_time` (07:00 default) → `silent` |
| `silent` | Já passou o `report_time` desde N1.1 | 30 dias seguidos sem queda → `quiet` |

### N1.1 — Primeira queda

Dispara quando a integração estava em `quiet` e acontece uma queda.

- **Título**: `[Integração] instável`
- **1 entidade**: `Entidade A caiu.`
- **≥2 entidades** (mesmo burst): `Entidade A e outras caíram.`

### N1.2 — Padrão de instabilidade

Dispara quando, durante `active_day1`, acontece uma 2ª queda **dentro da
janela do N1.2** (padrão 30 min) a partir da 1ª. **Uma única vez** por
período ativo.

- **Título**: `[Integração] instável`
- A entidade citada é a que **mais caiu** nas bursts do dia.
- **1 entidade afetada no total**: `Entidade A caiu X vezes nos últimos
  Y minutos.`
- **≥2 entidades**: `Entidade A e outras caíram X vezes nos últimos Y
  minutos.`
- X = nº de bursts até o disparo. Y = janela configurada.

### N2 — Relatório de quedas (diário)

Dispara no `report_time` (07:00 default) para cada integração que teve
**≥1 queda** no ciclo anterior (ciclo = das 07:00 de ontem às 06:59 de
hoje).

- **Título**: `Relatório [Integração]`
- **1 entidade afetada no total**: `Entidade A caiu X vezes ontem.`
- **≥2 entidades**: `Entidade A e outras caíram X vezes ontem.`
- A entidade citada é a que mais caiu no ciclo. X = total de bursts.

### N3.1 — Offline prolongado (em tempo real)

Dispara quando uma entidade fica offline **continuamente** por mais que o
**Limiar de offline N3** (padrão 30 min). **Independente** do estado N1.
Uma única vez por outage da integração — só rearma quando todas as
entidades dela voltarem ao normal.

- **Título**: `[Integração] offline por mais de 30 minutos`
- A entidade citada é a que está offline há mais tempo.
- **1 entidade afetada**: `Entidade A offline por 30 minutos.`
- **≥2 entidades**: `Entidade A e outras offline por 30 minutos.`

### N3.2 — Relatório de offline (diário)

Dispara no `report_time` para cada integração em que **pelo menos uma
entidade teve um único outage ≥ limiar N3** no ciclo anterior.

- **Título**: `[Integração] offline por {duração}`
- A entidade citada é a que mais acumulou offline.
- **1 entidade afetada**: `Entidade A ficou offline por {duração} ontem.`
- **≥2 entidades**: `Entidade A e outras ficaram offline por {duração}
  ontem.`
- `{duração}` é a **união** do tempo em que **pelo menos 1 entidade** da
  integração esteve offline (overlaps contam uma vez). Auto-formatado em
  `Xh Ym` ou `Xm`.

### Citação de entidade

Em todas as notificações **apenas uma entidade é citada** pelo nome. Se
houver outras envolvidas, vem `"e outras"` — sem listar quem. Isso é
intencional pra não inchar a notificação.

### Evento `entity_monitor_notification`

Cada notificação dispara o evento `entity_monitor_notification`:

| Campo | Descrição |
| --- | --- |
| `integration` | Slug (ex: `localtuya`). |
| `integration_name` | Nome amigável (ex: `Local Tuya`). |
| `kind` | `n1_1` / `n1_2` / `n2` / `n3_1` / `n3_2` / `test`. |
| `scope` | `entity` (1 entidade) ou `integration` (≥2). |
| `entity_id`, `entity_name` | Entidade citada no corpo. |
| `has_others` | `true` se houve "e outras". |
| `outage_count` | X em N1.2/N2. |
| `window_minutes` | Y em N1.2. |
| `threshold_seconds` | Limiar do N3.1. |
| `duration_seconds` | Duração-união do N3.2. |
| `title`, `message` | Conteúdo final. |

## Persistência

Tudo é salvo em disco:

- Estatísticas cumulativas (entidades e integrações).
- Estado N1/N3 por integração.
- Buffer do ciclo atual (bursts + intervalos de offline).
- `started_at` dos outages em curso — restart **nunca conta a partir do
  boot**, retoma do momento real da queda.

## Reset

Dois caminhos:

- **`Reset estatísticas`** — só zera contadores cumulativos. Estado N1/N3
  preservado.
- **`Reset tudo`** — zera estatísticas, estado, ciclo atual e timers de
  outages em curso. Botão `button.entity_monitor_reset_all` ou serviço
  `entity_monitor.reset_all`.

Reset automático: a cada `auto_reset_days` (padrão 30) tanto os contadores
cumulativos zeram quanto o estado `silent → quiet` reseta. `0` desliga.

## Testar a notificação

- **Botão** `button.entity_monitor_test_notification`.
- **Serviço** `entity_monitor.test_notification`.

Em ambos o evento `entity_monitor_notification` é disparado com
`kind: "test"` e `test: true`.

## Eventos disparados

- `entity_monitor_unavailable` — em cada cruzamento de limiar
  (`level=seconds` no `seconds_threshold`, `level=minutes` no
  `n3_minutes_threshold`).
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
| `n1_burst_window_minutes` | `30` | Y do N1.2 (0 desliga N1.2) |
| `n3_minutes_threshold` | `30` | Limiar N3.1 e inclusão N3.2 (0 desliga N3) |
| `report_time_hour` | `7` | Hora local dos relatórios diários (0-23) |
| `notify_service` | — | Serviço `notify.*` |
| `auto_reset_days` | `30` | Reset auto de stats e estado (`0` desliga) |

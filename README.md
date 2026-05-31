# Entity-Monitor

Integração customizada para o **Home Assistant** que monitora o status das suas
entidades, avisa quando elas ficam **indisponíveis** (`unavailable`) e gera um
relatório com as entidades e integrações que mais caem.

## O que ela faz

- Acompanha uma lista de entidades escolhida por você.
- Dispara um conjunto de notificações com 3 níveis de severidade (N1/N2/N3) —
  ver detalhes em [Notificações automáticas](#notificações-automáticas).
- Guarda o histórico de quedas (mesmo após reiniciar o Home Assistant):
  quantas vezes cada entidade caiu e por quanto tempo.
- Gera um **relatório** ranqueando as entidades e integrações que mais ficam
  offline, com a contagem de quedas e o tempo total indisponível.

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
   - **Integrações a monitorar** — escolha uma ou mais integrações (ex:
     `tuya`, `mqtt`, `zigbee2mqtt`) e **todas** as entidades delas entram no
     monitoramento automaticamente. Ideal para acompanhar hubs inteiros sem
     listar entidade por entidade.
   - **Só a entidade principal de cada dispositivo** *(padrão ligado)* —
     quando você escolhe uma integração, mantém apenas **uma** entidade por
     device (a "principal", normalmente a que tem o mesmo nome do dispositivo),
     descartando sensores de diagnóstico, RSSI, bateria, etc. Desligue se
     quiser monitorar cada entidade individualmente.
   - **Entidades extras** *(opcional)* — entidades avulsas que você quer
     monitorar mesmo que a integração delas não esteja selecionada.
   - **Confirmar queda após (segundos)** — quanto tempo a entidade precisa
     ficar `unavailable` para o sistema considerar uma queda confirmada.
     Padrão: `30`.
   - **Janela de quedas simultâneas (segundos)** — se várias entidades da
     mesma integração caírem dentro dessa janela, contam como **1 evento**
     (provavelmente o hub caiu). Padrão: `20`.
   - **Serviço de notificação** *(opcional)* — o serviço `notify.*` que vai
     receber os avisos (ex: `notify.mobile_app_meu_celular`). Deixe em branco
     para não enviar notificação automática (o evento ainda é disparado).
   - **Janela do resumo N2 / cooldown do N1** *(horas)* — tamanho da janela
     coberta pelo resumo N2 e período de silêncio do N1. Padrão: `24`.
   - **Janela do upgrade N1** *(horas, 0 desliga)* — durante esse tempo após
     o N1, uma 2ª entidade diferente caindo dispara a notificação de upgrade.
     Padrão: `2`.
   - **Limite curto N3** *(minutos, 0 desliga)* — uma entidade indisponível
     por esse tempo dispara o N3-short. Padrão: `30`.
   - **Limite longo N3** *(horas, 0 desliga)* — uma entidade indisponível
     por esse tempo dispara o N3-long. Padrão: `12`.
4. Pronto. Você pode mudar tudo a qualquer momento clicando em **Configurar**
   na integração (em *Configurações → Dispositivos e Serviços*).

## O que é criado

Um dispositivo **Entity Monitor** com as seguintes entidades:

| Entidade | Descrição |
| --- | --- |
| `binary_sensor.entity_monitor_problem` | Liga (`on`) enquanto qualquer entidade monitorada estiver indisponível. |
| `sensor.entity_monitor_total_outages` | Número total de quedas registradas. |
| `sensor.entity_monitor_total_downtime` | Tempo total offline (em minutos). |
| `sensor.entity_monitor_downtime_report` | O relatório completo nos atributos: `worst_entities` e `worst_integrations`. |
| `button.entity_monitor_test_notification` | Aperte para disparar uma notificação de teste pelo serviço configurado. |

## Quedas simultâneas (coalescência)

Quando um hub cai, dezenas de entidades da mesma integração ficam
indisponíveis quase ao mesmo tempo. Isso **não** deve contar como dezenas de
quedas — é **um único evento**.

Por isso, quedas de entidades da mesma integração que acontecem dentro da
**janela de quedas simultâneas** (padrão 20s) são agrupadas em um só evento
de queda (*burst*). Esse evento único é o que conta tanto na notificação
quanto no ranking de integrações do relatório.

O tempo offline de **cada entidade** continua sendo registrado
individualmente (você ainda vê quanto cada uma ficou fora no ranking por
entidade).

## Notificações automáticas

Se você preencher o **Serviço de notificação**, o Entity Monitor envia avisos
sozinho — sem precisar criar automação. A lógica em três níveis:

### N1 — Queda pontual (imediata)

Dispara quando uma integração cai e **não há ciclo ativo** pra ela. O
conteúdo varia conforme **quantas entidades** caíram juntas no mesmo
*burst*:

- **1 entidade** → título: `<Nome da integração>`, corpo:
  `<Nome amigável> ficou indisponível.`
- **2+ entidades** → título: `Integração <X> instável`, corpo:
  `Várias entidades caíram juntas.`

Depois disso, abre um ciclo de `notify_cooldown_hours` horas (padrão 24h)
em que o N1 fica silencioso. Durante esse ciclo:

- Se uma **segunda entidade diferente** cai dentro da
  `notify_upgrade_window_hours` (padrão 2h), uma notificação de upgrade é
  disparada — única vez por ciclo. Depois desse limite quedas adicionais
  só acumulam silenciosamente.
- Todas as quedas continuam sendo acumuladas para o resumo N2.

### N2 — Resumo periódico (rolling)

Ao final de cada janela de `notify_cooldown_hours` (padrão 24h), o sistema
emite um resumo do que aconteceu:

- Na **1ª janela** do ciclo (logo após o N1) ele só dispara se houve **≥2
  quedas**, porque o N1 já anunciou a primeira.
- Em janelas seguintes (ciclo rolando), basta **≥1 queda** pra disparar.

Formato:

- Se todas as quedas no período foram da mesma entidade (sem upgrade):
  `<Nome> indisponível X vezes nas últimas Yh.`
- Caso contrário:
  `X quedas nas últimas Yh.` (título `Integração <X> instável`)

**Rolling:** se houve ao menos 1 queda na janela, o ciclo continua e abre
uma nova janela silenciosa. Próximo N2 em +24h. Se uma janela inteira passa
sem nenhuma queda, o ciclo encerra — a próxima queda dispara um N1 fresh.
Assim, quando uma integração fica instável por vários dias seguidos, você
recebe **apenas um resumo por dia** (e não um N1 novo a cada manhã).

### N3 — Indisponibilidade prolongada (independente)

Notificações disparadas quando uma entidade fica offline por muito tempo,
**independentes** do ciclo N1/N2. Dois limiares:

- **N3-short** — `sustained_outage_short_minutes` minutos (padrão 30min)
- **N3-long** — `sustained_outage_long_hours` horas (padrão 12h)

Cada um dispara uma vez por outage da integração:

- Se só **uma entidade** estiver indisponível ≥ limiar:
  `<Nome> indisponível há mais de N minutos/horas.`
- Se **duas ou mais** entidades da mesma integração estiverem
  indisponíveis ≥ limiar:
  `Integração <X> indisponível há mais de N minutos/horas.` (promoção)

O estado N3 é resetado quando **todas** as entidades da integração voltam
ao normal. Coloque o valor em `0` para desligar.

### Evento `entity_monitor_notification`

Cada notificação dispara também o evento `entity_monitor_notification` no
bus do Home Assistant, com os campos:

| Campo | Descrição |
| --- | --- |
| `integration` | Slug da integração (ex: `localtuya`). |
| `integration_name` | Nome amigável (ex: `Local Tuya`). |
| `kind` | `n1` / `n1_upgrade` / `n2` / `n3_short` / `n3_long` / `test`. |
| `scope` | `entity` ou `integration`. |
| `entity_id` | ID da entidade quando `scope=entity`. |
| `entity_name` | Nome amigável da entidade. |
| `outage_count` | Para N2: número de quedas na janela. |
| `window_hours` | Para N2: tamanho da janela. |
| `threshold_seconds` | Para N3: limite que disparou. |
| `title`, `message` | Conteúdo final da notificação. |

### Testar a notificação

Para conferir se o serviço está configurado certo:

- **Botão** — abra o device *Entity Monitor* e clique em **Testar notificação**
  (`button.entity_monitor_test_notification`).
- **Serviço** — chame `entity_monitor.test_notification` em
  *Ferramentas para Desenvolvedores → Serviços*.

Em ambos os casos, uma notificação de exemplo cai no celular (se houver
serviço configurado) e o evento `entity_monitor_notification` é disparado
com `kind: test` e `test: true`, para você poder filtrar em automações.

## Eventos disparados

Use estes eventos em automações para integrar com Telegram, push, etc.:

- `entity_monitor_unavailable` — disparado nos limites de
  `seconds_threshold`, `sustained_outage_short_minutes` e
  `sustained_outage_long_hours`. Campos: `entity_id`, `friendly_name`,
  `integration`, `level` (`seconds`, `minutes` ou `hours`),
  `threshold_seconds`, `unavailable_since`, `duration_seconds`.
- `entity_monitor_recovered` — disparado quando a entidade volta ao normal.
  Campos: `entity_id`, `integration`, `duration_seconds`, `duration`,
  `outage_count`.
- `entity_monitor_notification` — disparado em cada notificação enviada
  (ver tabela acima).
- `entity_monitor_report` — disparado quando o serviço de relatório roda.

### Exemplo de automação (alerta de queda)

```yaml
automation:
  - alias: Avisar quando uma entidade cair
    trigger:
      - platform: event
        event_type: entity_monitor_unavailable
    action:
      - service: notify.notify
        data:
          title: >-
            {% if trigger.event.data.level == 'hours' %}
            Entidade offline há muitas horas
            {% elif trigger.event.data.level == 'minutes' %}
            Entidade offline há vários minutos
            {% else %}
            Entidade offline
            {% endif %}
          message: >-
            {{ trigger.event.data.friendly_name }}
            ({{ trigger.event.data.integration }}) está indisponível há
            {{ trigger.event.data.duration_seconds }}s.
```

## Relatório

Há duas formas de obter o relatório de quem mais fica offline:

**1. Pelo sensor** — abra `sensor.entity_monitor_downtime_report` e veja os
atributos `worst_entities` (ranking por entidade) e `worst_integrations`
(ranking por integração).

**2. Pelo serviço** — chame `entity_monitor.generate_report`. Em
**Ferramentas para Desenvolvedores → Serviços**, marque *retornar resposta*
para ver o relatório na hora. Ele traz, ordenado da pior para a melhor:

- `by_entity` — cada entidade com `outage_count` (nº de quedas da entidade),
  `total_downtime` (tempo total offline) e `longest_outage` (maior queda).
- `by_integration` — por integração, com `outage_count` contando **eventos
  de queda** (quedas simultâneas já agrupadas) e `total_downtime` do período
  em que a integração esteve fora.

Para zerar o histórico:

- **Manualmente** — chame o serviço `entity_monitor.reset_statistics` ou
  rode a partir de uma automação.
- **Automaticamente** — a cada `auto_reset_days` dias (padrão 30) o
  Entity Monitor zera os contadores sozinho, dando uma janela móvel.
  Coloque `0` para desligar. O relatório inclui `last_reset_at` mostrando
  quando foi o último reset.

## Configuração

| Campo | Significado | Padrão |
| --- | --- | --- |
| `integrations` | Integrações cujas entidades são monitoradas inteiras | — |
| `only_primary_entity` | Manter só a entidade principal de cada device | `true` |
| `entities` | Entidades avulsas extras | — |
| `seconds_threshold` | Segundos para confirmar a queda | `30` |
| `coalesce_seconds` | Janela em que quedas simultâneas viram 1 evento | `20` |
| `notify_service` | Serviço `notify.*` para os avisos automáticos (opcional) | — |
| `notify_cooldown_hours` | Janela do resumo N2 e cooldown do N1 (horas) | `24` |
| `notify_upgrade_window_hours` | Janela do upgrade N1 (horas, 0 desliga) | `2` |
| `sustained_outage_short_minutes` | Limite curto do N3 (minutos, 0 desliga) | `30` |
| `sustained_outage_long_hours` | Limite longo do N3 (horas, 0 desliga) | `12` |
| `auto_reset_days` | Zera as estatísticas a cada N dias (`0` desliga) | `30` |

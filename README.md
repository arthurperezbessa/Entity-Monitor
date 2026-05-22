# Entity-Monitor

Integração customizada para o **Home Assistant** que monitora o status das suas
entidades e avisa quando elas ficam **indisponíveis** (`unavailable`), além de
gerar um relatório com as entidades e integrações que mais caem.

## O que ela faz

- Acompanha uma lista de entidades escolhida por você.
- Dispara um **alerta curto** quando uma entidade fica indisponível por mais de
  `X segundos`.
- Dispara um **alerta longo** quando a entidade continua indisponível por mais
  de `X minutos`.
- Guarda o histórico de quedas (mesmo após reiniciar o Home Assistant): quantas
  vezes cada entidade caiu e por quanto tempo.
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
   - **Entidades a monitorar** — selecione a lista de entidades que você quer
     acompanhar (sensores, luzes, câmeras, dispositivos de qualquer integração).
   - **Limite do alerta curto (segundos)** — o `X segundos`. Padrão: `30`.
   - **Limite do alerta longo (minutos)** — o `X minutos`. Padrão: `5`.
4. Pronto. Você pode mudar a lista de entidades e os limites a qualquer momento
   em **Configurar** na própria integração.

## O que é criado

Um dispositivo **Entity Monitor** com as seguintes entidades:

| Entidade | Descrição |
| --- | --- |
| `binary_sensor.entity_monitor_problem` | Liga (`on`) enquanto qualquer entidade monitorada estiver indisponível. |
| `sensor.entity_monitor_total_outages` | Número total de quedas registradas. |
| `sensor.entity_monitor_total_downtime` | Tempo total offline (em minutos). |
| `sensor.entity_monitor_downtime_report` | O relatório completo nos atributos: `worst_entities` e `worst_integrations`. |

## Eventos disparados

Use estes eventos em automações para notificar você (Telegram, push, etc.):

- `entity_monitor_unavailable` — disparado nos limites de segundos **e** de
  minutos. Campos: `entity_id`, `friendly_name`, `integration`, `level`
  (`seconds` ou `minutes`), `threshold_seconds`, `unavailable_since`,
  `duration_seconds`.
- `entity_monitor_recovered` — disparado quando a entidade volta ao normal.
  Campos: `entity_id`, `integration`, `duration_seconds`, `duration`,
  `outage_count`.
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
            {% if trigger.event.data.level == 'minutes' %}
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

- `by_entity` — cada entidade com `outage_count` (nº de quedas),
  `total_downtime` (tempo total offline) e `longest_outage` (maior queda).
- `by_integration` — o mesmo agregado por integração.

Para zerar o histórico, chame o serviço `entity_monitor.reset_statistics`.

## Configuração

| Campo | Significado | Padrão |
| --- | --- | --- |
| `entities` | Lista de entidades monitoradas | — |
| `seconds_threshold` | `X segundos` para o alerta curto | `30` |
| `minutes_threshold` | `X minutos` para o alerta longo | `5` |

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
   - **Limite do alerta curto (segundos)** — o `X segundos`. Padrão: `30`.
   - **Limite do alerta longo (minutos)** — o `X minutos`. Padrão: `5`.
   - **Janela de quedas simultâneas (segundos)** — se várias entidades da
     mesma integração caírem dentro dessa janela, contam como **1 evento**
     (provavelmente o hub caiu). Padrão: `20`.
   - **Serviço de notificação** *(opcional)* — o serviço `notify.*` que vai
     receber os avisos (ex: `notify.mobile_app_meu_celular`). Deixe em branco
     para não enviar notificação automática (o evento ainda é disparado).
   - **Esperar para notificar de novo (horas)** — o intervalo de silêncio
     antes de avisar outra vez sobre a mesma integração. Padrão: `1`.
4. Pronto. Você pode mudar as integrações, entidades, limites e notificações
   a qualquer momento clicando em **Configurar** na integração (em
   *Configurações → Dispositivos e Serviços → Integrations*).

## O que é criado

Um dispositivo **Entity Monitor** com as seguintes entidades:

| Entidade | Descrição |
| --- | --- |
| `binary_sensor.entity_monitor_problem` | Liga (`on`) enquanto qualquer entidade monitorada estiver indisponível. |
| `sensor.entity_monitor_total_outages` | Número total de quedas registradas. |
| `sensor.entity_monitor_total_downtime` | Tempo total offline (em minutos). |
| `sensor.entity_monitor_downtime_report` | O relatório completo nos atributos: `worst_entities` e `worst_integrations`. |

## Quedas simultâneas (coalescência)

Quando um hub cai, dezenas de entidades da mesma integração ficam
indisponíveis quase ao mesmo tempo. Isso **não** deve contar como dezenas de
quedas — é **um único evento**.

Por isso, quedas de entidades da mesma integração que acontecem dentro da
**janela de quedas simultâneas** (padrão 20s) são agrupadas em um só evento de
queda (*burst*). Esse evento único é o que conta tanto na notificação quanto
no ranking de integrações do relatório. Exemplo: 30 entidades da `tuya` caem
em 8 segundos → conta como **1 queda da integração tuya**, não 30.

O tempo offline de **cada entidade** continua sendo registrado individualmente
(você ainda vê quanto cada uma ficou fora no ranking por entidade).

## Notificações automáticas

Se você preencher o **Serviço de notificação**, o Entity Monitor envia avisos
sozinho — sem precisar criar automação. A lógica:

- Quando uma integração cai, o aviso é enviado logo após a janela de quedas
  simultâneas fechar (assim ele já inclui todas as entidades que caíram juntas).
- Se a mesma **integração** cair de novo, o aviso fica em silêncio durante o
  período configurado (**X horas**). As quedas continuam sendo contadas.
- Passadas as X horas, a próxima queda dispara um novo aviso mostrando
  **quantas vezes** a integração caiu desde o último aviso.

O agrupamento é **uma linha por integração**, para não ser redundante:

- Se só **uma entidade** da integração caiu:
  `Luz Varanda ficou indisponível 3 vezes.`
- Se **várias entidades** da mesma integração caíram juntas (mesmo hub):
  `Integração tuya: 30 entidades caíram juntas. ...`
- Integrações **diferentes** têm avisos separados — se o ar-condicionado e a
  luz da varanda caírem, você recebe um aviso de cada um.

Cada aviso também dispara o evento `entity_monitor_notification` (campos:
`integration`, `entity_ids`, `entity_names`, `outage_events`, `entity_count`,
`title`, `message`), caso você prefira tratá-lo numa automação própria.

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

- `by_entity` — cada entidade com `outage_count` (nº de quedas da entidade),
  `total_downtime` (tempo total offline) e `longest_outage` (maior queda).
- `by_integration` — por integração, com `outage_count` contando **eventos de
  queda** (quedas simultâneas já agrupadas) e `total_downtime` do período em
  que a integração esteve fora.

Para zerar o histórico, chame o serviço `entity_monitor.reset_statistics`.

## Configuração

| Campo | Significado | Padrão |
| --- | --- | --- |
| `integrations` | Integrações cujas entidades são monitoradas inteiras | — |
| `only_primary_entity` | Manter só a entidade principal de cada device | `true` |
| `entities` | Entidades avulsas extras | — |
| `seconds_threshold` | `X segundos` para o alerta curto | `30` |
| `minutes_threshold` | `X minutos` para o alerta longo | `5` |
| `coalesce_seconds` | Janela em que quedas simultâneas viram 1 evento | `20` |
| `notify_service` | Serviço `notify.*` para os avisos automáticos (opcional) | — |
| `renotify_hours` | Horas de silêncio antes de avisar a mesma integração | `1` |

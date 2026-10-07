"""Livro de movimentos por etapa (EMPENHO, DESEMBOLSO, PAGAMENTO) de TC/Convênio.

Regra do usuário (24/09/2026): cada documento é uma ENTRADA (+) ou uma SAÍDA (-). Ex.: empenho de
500 mil e depois anulação de 500 mil = +500 mil e -500 mil, saldo zero. Documento substituído,
cancelado ou não utilizado por qualquer motivo NÃO entra no total, mas continua LISTADO (com o
motivo) no botão da etapa. Cada linha:
  {documento, data, movimento: "Entrada"|"Saída", valor (com sinal), considerado: bool,
   situacao: texto, fonte, _chave (NE/OB curta)}
Total da etapa = soma de `valor` das linhas com considerado=True.

Fontes (ver scripts/criar_tg_execucao_join.py para os eventos do Tesouro Gerencial):
  EMPENHO    tg_execucao_join etapa EMPENHO (eventos; '*_ANULACAO' = saída), agrupado pela NE de
             referência; SICONV (transferegov_siconv_empenho) só para NE ausente no TG; SALDO_NE
             (UG 110591) só para NE ausente nas duas. O `valor_empenho` do SICONV não é confiável
             (34 de 85 NEs divergem do TG: às vezes é o saldo a pagar) — quando há SALDO_NE > 0 da
             mesma NE (UG 110591), ele é o valor usado.
  DESEMBOLSO OB da UG do FNAC (110591) em tg_execucao_join etapa PAGAMENTO (estorno = saída) +
             transferegov_siconv_desembolso para OB ausente no TG. CONTRATO: toda OB ao contratado, do FNAC
             ou da UG executora do crédito descentralizado (livro_desembolso(todas_ug=True)).
  Regra padronizada (usuário, 25/09/2026): DESEMBOLSO = recurso ao EXECUTOR (convenente, UG do TED, contratado,
  BNDES); PAGAMENTO = o executor paga o PARTICULAR (fornecedor; no BNDES, empréstimo às aéreas).
             GRU de devolução do convenente (tg_execucao_join etapa GRU, radar_gru.csv, 27/09/2026) = saída; GRU de
             rendimento de aplicação financeira = listada, fora do saldo.
  PAGAMENTO  transferegov_siconv_pagamento + OBTV do Tesouro Gerencial (OB de outra UG), sem
             repetição: mesma OBTV reconhecida por valor no mesmo ano (ver livro_pagamento).
Regra geral (usuário, 24/09/2026): as fontes são SEMPRE complementares — trazer todos os
documentos distintos de todas as fontes, contando uma única vez o que existe em mais de uma.
"""
from __future__ import annotations

import re
import sqlite3

RE_NE = re.compile(r"(\d{4}NE\d+)")
RE_OB = re.compile(r"(\d{4}OB\d+)")
UG_FNAC = "110591"

# Documentos que não foram utilizados sem que exista documento de saída na base (decisão do usuário).
# Chave: (processo_sei, etapa, NE/OB curta) -> motivo com a fonte.
DOCUMENTOS_NAO_UTILIZADOS = {
    ("50020.006318/2023-39", "EMPENHO", "2023NE000074"):
        "Substituída pela 2026NE000028 (decisão do usuário, 24/09/2026). No SIAFI segue em Restos a Pagar "
        "sem cancelamento (db_bi_tesgerencial.csv, localizador 15YT5230, item 53, jan/2026: R$ 7.002.198,35).",
    ("50020.006318/2023-39", "EMPENHO", "2023NE000075"):
        "Substituída pela 2026NE000028 (decisão do usuário, 24/09/2026). No SIAFI segue em Restos a Pagar "
        "sem cancelamento (db_bi_tesgerencial.csv, localizador 15YT5230, item 53, jan/2026: R$ 8.439.489,06).",
}


# Documentos que CONTINUAM considerados, mas com anotação da decisão do usuário (aparece em 'situacao').
# Chave: (processo_sei, etapa, NE/OB curta) -> anotação com a fonte.
DOCUMENTOS_ANOTADOS = {
    ("50000.013045/2017-79", "EMPENHO", "2017NE000074"):
        "executada na 1ª fase do TC 01/2017 (Serra Talhada/PE) — decisão do usuário, 24/09/2026. Sem cancelamento nos eventos do SIAFI "
        "(arquivos 'NE ate 2020' e 'RO-NE com evento' 2021-2026); o extrato de pagamentos de 2017 não está nas bases (SICONV não tem o TC; "
        "radar_ob.csv só cobre 2026). Somada às NEs 2024NE000007 (16,5 mi) e 2025NE000026 (4,65 mi), o empenho (26,15 mi) excede o valor do TC (20 mi).",
    ("50020.007367/2024-70", "EMPENHO", "2024NE000031"):
        "ato preliminar à celebração do TC 969243/2024 (Caruaru/PE), sem repasse. Impressão SIAFI da NE (consulta de 05/03/2026, "
        "doc. SIAFI-2024NE000031): emitida em 24/12/2024 por R$ 15.400.839,00, reinscrita em Restos a Pagar em 18/01/2026 e CANCELADA "
        "em 03/03/2026 (valor atual 0,00) — cancelamento recomendado pela NT 4/2026 (doc. 10822528): denúncia e extinção do TC.",
    ("50000.008130/2019-87", "EMPENHO", "2019NE000097"):
        "TC 05/2019 (Barra do Garças/MT) extinto por inexecução e decurso de prazo; a NE 'não teve execução financeira' e teve o "
        "cancelamento recomendado (Parecer 10/2022, doc. 6598122). A 2019NE000095 (mesmo valor) foi anulada pela 2019NE000096 "
        "('anulação para ajuste da descrição': citava Ponta Grossa).",
    ("50000.007129/2019-35", "EMPENHO", "2019NE000131"):
        "TC 15/2019 (Jijoca/CE): R$ 788.000,00 (600.000,00 da emissão + 188.000,00 do reforço 2019NE000245), bloqueada e cancelada sem "
        "repasse — o compromissário não abriu conta específica; NT 4434159 propõe a denúncia e rescisão.",
    ("50000.032985/2020-62", "EMPENHO", "2021NE000003"):
        "impressão SIAFI (doc. SIAFI-2021NE000003): emitida em 22/02/2021 (estimativo, R$ 100.000,00); anulada em 24/02/2021 por indisponibilidade de caixa e reemitida como 2021NE000004",
    ("50000.032985/2020-62", "EMPENHO", "2021NE000004"):
        "NE da dotação original do Contrato 06/2021 (cláusula 4.1, doc. 3810122, grafada '2121NE000004'); RO 2021RO000005 de 01/03/2021 (doc. SIAFI-2021NE000004)",
    ("50000.032985/2020-62", "EMPENHO", "2021NE000042"):
        "impressão SIAFI (doc. SIAFI-2021NE000042); anulada em 07/12/2021 e reemitida no mesmo dia como 2021NE000045",
    ("50000.032985/2020-62", "EMPENHO", "2021NE000053"):
        "impressão SIAFI (doc. SIAFI-2021NE000053): 10/12/2021, R$ 450.000,00",
    ("50000.032985/2020-62", "EMPENHO", "2021NE000060"):
        "impressão SIAFI (doc. SIAFI-2021NE000060): 22/12/2021, R$ 1.000.000,00, Itacoatiara",
    ("50000.032985/2020-62", "EMPENHO", "2023NE000050"):
        "Itacoatiara — valor da NE na dotação de 2025 do 5º Termo Aditivo (doc. 10176614, cláusula 3): R$ 3.443.524,12 (emitida com 9.448.639,00; a diferença foi liquidada/paga ou cancelada — eventos fora das bases)",
    ("50000.032985/2020-62", "EMPENHO", "2023NE000012"):
        "Maués — valor da NE na dotação de 2025 do 5º Termo Aditivo (doc. 10176614, cláusula 3): R$ 3.402.419,66 (emitida com 3.953.737,00)",
    ("50000.032985/2020-62", "EMPENHO", "2023NE000049"):
        "Maués — valor da NE na dotação de 2025 do 5º Termo Aditivo (doc. 10176614, cláusula 3): R$ 5.425.781,00 (igual ao emitido)",
    ("50000.032985/2020-62", "EMPENHO", "2023NE000031"):
        "Fonte Boa — valor da NE na dotação de 2025 do 5º Termo Aditivo (doc. 10176614, cláusula 3): R$ 2.687.647,46 (emitida com 6.544.074,00)",
    ("50000.032985/2020-62", "EMPENHO", "2023NE000032"):
        "Fonte Boa — valor da NE na dotação de 2025 do 5º Termo Aditivo (doc. 10176614, cláusula 3): R$ 312.944,00 (igual ao emitido)",
    ("50000.032985/2020-62", "EMPENHO", "2023NE000041"):
        "Fonte Boa — valor da NE na dotação de 2025 do 5º Termo Aditivo (doc. 10176614, cláusula 3): R$ 610.451,00 (igual ao emitido)",
    ("50000.032985/2020-62", "EMPENHO", "2023NE000042"):
        "Fonte Boa — valor da NE na dotação de 2025 do 5º Termo Aditivo (doc. 10176614, cláusula 3): R$ 9.201.745,00 (igual ao emitido)",
}

# Movimentos que só existem em DOCUMENTO (nenhuma base do Tesouro Gerencial/SICONV/Transferegov os traz). Fonte
# complementar: entram só se o mesmo documento (chave) e movimento ainda não estiverem no livro.
# {processo: [(etapa, chave, documento, data ISO, valor com sinal, fonte)]}
MOVIMENTOS_DOCUMENTO = {
    # ===== NE FICTÍCIA 2017NE000000 (usuário, 04/10/2026) — 4 parcelas, uma por TC; total R$ 7.290.392,43 (as outras 8 viraram NE real) =====
    "00055.001641/2016-11": [   # TC nº 02/2017 Maringá/PR — NE fictícia 2017NE000000, parcela 1/4
        ("EMPENHO", "2017NE000000", '2017NE000000 — EMPENHO FICTÍCIO para suprir falta de dados — parcela 1/4, atende o TC nº 02/2017 Maringá/PR (processo 00055.001641/2016-11, SIAFI 690489): R$ 3.742.316,82. NE total R$ 7.290.392,43, abrange: TC nº 02/2017 Maringá/PR (proc. 00055.001641/2016-11, SIAFI 690489): R$ 3.742.316,82; TC nº 07/2017 Cacoal/RO (proc. 50000.035108/2017-48, SIAFI 692418): R$ 2.319.524,01; TC nº 02/2018 Santa Rosa/RS (proc. 71000.000214/2018-61, SIAFI 697076): R$ 769.952,35; TC nº 03/2020 Divinópolis/MG (proc. 50000.003273/2020-36, SIAFI 1AAAPN): R$ 458.599,25', "2017-08-18", 3742316.82,
         'EMPENHO FICTÍCIO (decisão do usuário, 04/10/2026): supre empenhos anteriores a 2021 que não estão nas bases do Tesouro Gerencial (o desembolso do DINV ficou maior que o empenho). Data = assinatura do instrumento. SUBSTITUIR pelas NEs reais quando vierem'),
    ],
    "50000.035108/2017-48": [   # TC nº 07/2017 Cacoal/RO — NE fictícia 2017NE000000, parcela 2/4
        ("EMPENHO", "2017NE000000", '2017NE000000 — EMPENHO FICTÍCIO para suprir falta de dados — parcela 2/4, atende o TC nº 07/2017 Cacoal/RO (processo 50000.035108/2017-48, SIAFI 692418): R$ 2.319.524,01. NE total R$ 7.290.392,43, abrange: TC nº 02/2017 Maringá/PR (proc. 00055.001641/2016-11, SIAFI 690489): R$ 3.742.316,82; TC nº 07/2017 Cacoal/RO (proc. 50000.035108/2017-48, SIAFI 692418): R$ 2.319.524,01; TC nº 02/2018 Santa Rosa/RS (proc. 71000.000214/2018-61, SIAFI 697076): R$ 769.952,35; TC nº 03/2020 Divinópolis/MG (proc. 50000.003273/2020-36, SIAFI 1AAAPN): R$ 458.599,25', "2017-12-11", 2319524.01,
         'EMPENHO FICTÍCIO (decisão do usuário, 04/10/2026): supre empenhos anteriores a 2021 que não estão nas bases do Tesouro Gerencial (o desembolso do DINV ficou maior que o empenho). Data = assinatura do instrumento. SUBSTITUIR pelas NEs reais quando vierem'),
    ],
    "71000.000214/2018-61": [   # TC nº 02/2018 Santa Rosa/RS — NE fictícia 2017NE000000, parcela 3/4
        ("EMPENHO", "2017NE000000", '2017NE000000 — EMPENHO FICTÍCIO para suprir falta de dados — parcela 3/4, atende o TC nº 02/2018 Santa Rosa/RS (processo 71000.000214/2018-61, SIAFI 697076): R$ 769.952,35. NE total R$ 7.290.392,43, abrange: TC nº 02/2017 Maringá/PR (proc. 00055.001641/2016-11, SIAFI 690489): R$ 3.742.316,82; TC nº 07/2017 Cacoal/RO (proc. 50000.035108/2017-48, SIAFI 692418): R$ 2.319.524,01; TC nº 02/2018 Santa Rosa/RS (proc. 71000.000214/2018-61, SIAFI 697076): R$ 769.952,35; TC nº 03/2020 Divinópolis/MG (proc. 50000.003273/2020-36, SIAFI 1AAAPN): R$ 458.599,25', "2018-09-11", 769952.35,
         'EMPENHO FICTÍCIO (decisão do usuário, 04/10/2026): supre empenhos anteriores a 2021 que não estão nas bases do Tesouro Gerencial (o desembolso do DINV ficou maior que o empenho). Data = assinatura do instrumento. SUBSTITUIR pelas NEs reais quando vierem'),
    ],
    "50000.003273/2020-36": [   # TC nº 03/2020 Divinópolis/MG — NE fictícia 2017NE000000, parcela 4/4
        ("EMPENHO", "2017NE000000", '2017NE000000 — EMPENHO FICTÍCIO para suprir falta de dados — parcela 4/4, atende o TC nº 03/2020 Divinópolis/MG (processo 50000.003273/2020-36, SIAFI 1AAAPN): R$ 458.599,25. NE total R$ 7.290.392,43, abrange: TC nº 02/2017 Maringá/PR (proc. 00055.001641/2016-11, SIAFI 690489): R$ 3.742.316,82; TC nº 07/2017 Cacoal/RO (proc. 50000.035108/2017-48, SIAFI 692418): R$ 2.319.524,01; TC nº 02/2018 Santa Rosa/RS (proc. 71000.000214/2018-61, SIAFI 697076): R$ 769.952,35; TC nº 03/2020 Divinópolis/MG (proc. 50000.003273/2020-36, SIAFI 1AAAPN): R$ 458.599,25', "2020-05-25", 458599.25,
         'EMPENHO FICTÍCIO (decisão do usuário, 04/10/2026): supre empenhos anteriores a 2021 que não estão nas bases do Tesouro Gerencial (o desembolso do DINV ficou maior que o empenho). Data = assinatura do instrumento. SUBSTITUIR pelas NEs reais quando vierem'),
    ],
    "00055.002207/2012-25": [   # TC 777036/12 (DAESP, 6 aeroportos) — empenho acima da União (SICONV vl_empenhado_conv 8.137.548)
        ("EMPENHO", "2012NE800046-CANC", "2012NE800046 — cancelamento (R$ 3.000.000,00): o SICONV fecha o empenhado do convênio em R$ 8.137.548,00 "
         "= 2012NE800032 (6.405.000,00) + 2015NE800017 (1.732.548,00)", "2015-06-01", -3000000.00,
         "decisão do usuário (04/10/2026, check-up): 'cancele os 3mi' — sem tela de confirmação; data adotada = emissão da 2015NE800017"),
    ],
    # cancelamentos de RP de 31/12/2020 com evento 401108 (sinal 0 no Tesouro Gerencial; os de evento 401118 já entram pela base).
    # Bonito (CV 839130) e Coxim (CV 839133) também têm 401108, mas foram REEMPENHADOS (2020NE800020/800018) — ficam de fora.
    "50000.025244/2017-20": [   # Passo Fundo, TC 05/2017 — sobra da NE = 1ª + 2ª parcelas (DINV) = R$ 5.385.808,37
        ("EMPENHO", "2020NE800023", "2017NE000104 — cancelamento de RP 110591000012020NE800023", "2020-12-31", -4014191.63, "Tesouro Gerencial — 'RADAR - NE ate 2020 - Anos 2018 a 2020.csv': evento 401108 'CONT EMP-RPNP LIQUIDADO PAGO' (sinal 0 na base), descrição 'CANCELAMENTO DOS RESTOS A PAGAR NAO PROCESSADOS... ATE O DIA 31 DE DEZEMBRO DO ANO SUBSEQUENTE AO DO BLOQUEIO' — contado como cancelamento por decisão do usuário (04/10/2026, check-up)"),
    ],
    "50000.013045/2017-79": [   # Serra Talhada, TC 01/2017 — sobra da NE = 1ª parcela (10/07/2018, DINV) = R$ 2.000.000,00
        ("EMPENHO", "2020NE800021", "2017NE000074 — cancelamento de RP 110591000012020NE800021", "2020-12-31", -6000000.00, "Tesouro Gerencial — 'RADAR - NE ate 2020 - Anos 2018 a 2020.csv': evento 401108 'CONT EMP-RPNP LIQUIDADO PAGO' (sinal 0 na base), descrição 'CANCELAMENTO DOS RESTOS A PAGAR NAO PROCESSADOS... ATE O DIA 31 DE DEZEMBRO DO ANO SUBSEQUENTE AO DO BLOQUEIO' — contado como cancelamento por decisão do usuário (04/10/2026, check-up)"),
        ("EMPENHO", "2025NE000026-SALDO", "2025NE000026 — cancelamento do saldo acima do que falta pagar (União 20.000.000,00; pago "
         "15.115.161,36; saldo 2024NE000007 3.384.838,64 + 2025NE000026 4.650.829,00)", "2025-12-31", -3150829.00,
         "decisão do usuário (04/10/2026, check-up): saldo considerado CANCELADO sem tela de confirmação; data não informada (adotado 31/12/2025)"),
    ],
    "50000.006179/2017-33": [   # Bom Jesus, TC 04/2017
        ("EMPENHO", "EXCESSO-EMPENHO-BOMJESUS", "Cancelamento do empenho acima da União (União 21.591.273,04; empenhado 23.399.820,47) — NE não "
         "identificada (pagamentos antigos vêm do DINV sem NE; suspeita: 2018NE000192, R$ 2,0 mi sem pagamento identificado)", "2025-12-31", -1808547.43,
         "decisão do usuário (04/10/2026, check-up): excesso de empenho sobre a parte da União considerado CANCELADO sem tela de confirmação; data não informada (adotado 31/12/2025)"),
        ("EMPENHO", "2020NE800022", "2017NE000092 — cancelamento de RP 110591000012020NE800022", "2020-12-31", -4765689.00, "Tesouro Gerencial — 'RADAR - NE ate 2020 - Anos 2018 a 2020.csv': evento 401108 'CONT EMP-RPNP LIQUIDADO PAGO' (sinal 0 na base), descrição 'CANCELAMENTO DOS RESTOS A PAGAR NAO PROCESSADOS... ATE O DIA 31 DE DEZEMBRO DO ANO SUBSEQUENTE AO DO BLOQUEIO' — contado como cancelamento por decisão do usuário (04/10/2026, check-up)"),
    ],
    "50000.035020/2017-26": [   # Jataí, TC 15/2017
        ("EMPENHO", "2023NE000013-SALDO", "2023NE000013 — cancelamento do empenho acima da União (União 40.500.000,00; empenhado 48.449.900,13; "
         "saldo da 2023NE000013 R$ 19.035.948,18)", "2025-12-31", -7949900.13, "decisão do usuário (04/10/2026, check-up): excesso de empenho sobre a parte da União considerado CANCELADO sem tela de confirmação; data não informada (adotado 31/12/2025)"),
        ("EMPENHO", "2020NE800024", "2017NE000141 — cancelamento de RP 110591000012020NE800024", "2020-12-31", -2389707.87, "Tesouro Gerencial — 'RADAR - NE ate 2020 - Anos 2018 a 2020.csv': evento 401108 'CONT EMP-RPNP LIQUIDADO PAGO' (sinal 0 na base), descrição 'CANCELAMENTO DOS RESTOS A PAGAR NAO PROCESSADOS... ATE O DIA 31 DE DEZEMBRO DO ANO SUBSEQUENTE AO DO BLOQUEIO' — contado como cancelamento por decisão do usuário (04/10/2026, check-up)"),
    ],
    "50000.015011/2022-86": [   # Guarujá, TC 936826/2022 — saldo das NEs além do repasse (União R$ 16.090.632,61, toda desembolsada)
        ("EMPENHO", "2022NE000027", "2022NE000027 — cancelamento do saldo não utilizado (empenhado 10.000.000,00; pago 7.315.571,44 pelas "
         "2024OB000018, 2024OB000061 e 2025OB000010)", "2025-12-31", -2684428.56,
         "decisão do usuário (04/10/2026, check-up): saldo considerado CANCELADO sem tela de confirmação; data do evento não informada "
         "(adotado 31/12/2025). Saldos somados = R$ 4.871.492,02 = 1 parcela do cronograma do PT do 4º TA (SEI 11368270)"),
        ("EMPENHO", "2023NE000054", "2023NE000054 — cancelamento do saldo não utilizado (empenhado 8.140.005,00; pago 5.952.941,54 pelas "
         "2024OB000060, 2025OB000009 e 2025OB000034)", "2025-12-31", -2187063.46,
         "decisão do usuário (04/10/2026, check-up): saldo considerado CANCELADO sem tela de confirmação; data do evento não informada "
         "(adotado 31/12/2025)"),
    ],
    "00055.001847/2011-37": [   # Linhares/ES, CV 761964/2011 — relatório de PC do Transferegov (TRANSFEREGOV-PC-CV-761964-2023-03-09)
        ("DESEMBOLSO", "GRU-LINHARES-REPASSE", "GRU 28895-0 — devolução ao FNAC do saldo de repasse (PC do Convênio 761964/2011)", "2022-12-22", -1509916.01,
         "relatório de PC do Transferegov (09/03/2023), pág. 6: GRU código 28895-0, venc. 22/12/2022, R$ 1.509.916,01 = saldo de REPASSE "
         "(OB 2022OB01627 'Repasse'); as GRUs 48802-0 de R$ 2.759.811,11 e R$ 223.956,41 são rendimentos (OBs 01628/01652) e não entram "
         "(mesma regra de Barreirinhas). Identificação da GRU de repasse por inferência (código distinto), confirmada pelo usuário (opção A, 02/10/2026)"),
    ],
    "50000.023155/2018-20": [   # Barreirinhas/MA, TC 07/2018 — NT 4454643 (12/08/2021), itens 3.17, 3.19 e 3.20
        ("DESEMBOLSO", "2021OB800013", "2021OB800013 — 1ª parcela (parcial) ao compromissário", "2021-04-25", 1004819.00,
         "NT 4454643, item 3.17 (OB 2021OB8000013, SEI 4627079)"),
        ("DESEMBOLSO", "GRU-BARREIRINHAS", "GRU — devolução integral da parcela (rescisão pedida pelo Estado)", "2021-08-12", -1004819.00,
         "NT 4454643, itens 3.19-3.20 (GRU SEI 4169872; comprovante SEI 4391812); data = data da NT (a GRU é anterior); "
         "rendimentos de R$ 1.852,30 devolvidos em GRU à parte (não são repasse)"),
    ],
}


# Complemento até o VALOR ORIGINAL de NEs antigas: nos arquivos 'RADAR - NE ate 2020', 'NE - Valor' é o saldo na data da extração; o empenho
# real é o item ORIGINAL ('NE Item - Valor Total'). Só NEs com saldo > 0 (as de saldo zero podem ter sido canceladas — conferir caso a caso).
# Regra aprovada pelo usuário (04/10/2026, check-up). {processo: [(etapa, chave, documento, data, valor, fonte)]}
NES_VALOR_ORIGINAL = {
    "50000.006191/2019-18": [
        ('EMPENHO', '2020NE000013-ORIGINAL', "2020NE000013 — complemento até o valor ORIGINAL do empenho (item original R$ 3.000.000,00; o 'NE - Valor' do arquivo, R$ 1.473.954,14, é o saldo na data da extração)", '2020-06-30', 1526045.86, "Tesouro Gerencial — 'RADAR - NE ate 2020 - Ano 2020.csv', coluna 'NE Item - Valor Total' (espécie ORIGINAL); regra aprovada pelo usuário (04/10/2026, check-up)"),
    ],
    "50000.007087/2019-32": [
        ('EMPENHO', '2019NE000055-ORIGINAL', "2019NE000055 — complemento até o valor ORIGINAL do empenho (item original R$ 500.000,00; o 'NE - Valor' do arquivo, R$ 45.800,00, é o saldo na data da extração)", '2019-06-26', 454200.0, "Tesouro Gerencial — 'RADAR - NE ate 2020 - Ano 2016 a 2019.csv', coluna 'NE Item - Valor Total' (espécie ORIGINAL); regra aprovada pelo usuário (04/10/2026, check-up)"),
    ],
    "50000.008123/2019-85": [
        ('EMPENHO', '2019NE000099-ORIGINAL', "2019NE000099 — complemento até o valor ORIGINAL do empenho (item original R$ 3.000.000,00; o 'NE - Valor' do arquivo, R$ 600.000,00, é o saldo na data da extração)", '2019-09-24', 2400000.0, "Tesouro Gerencial — 'RADAR - NE ate 2020 - Ano 2016 a 2019.csv', coluna 'NE Item - Valor Total' (espécie ORIGINAL); regra aprovada pelo usuário (04/10/2026, check-up)"),
    ],
    "50000.022564/2019-90": [
        ('EMPENHO', '2019NE000127-ORIGINAL', "2019NE000127 — complemento até o valor ORIGINAL do empenho (item original R$ 500.000,00; o 'NE - Valor' do arquivo, R$ 25.000,00, é o saldo na data da extração)", '2019-11-26', 475000.0, "Tesouro Gerencial — 'RADAR - NE ate 2020 - Ano 2016 a 2019.csv', coluna 'NE Item - Valor Total' (espécie ORIGINAL); regra aprovada pelo usuário (04/10/2026, check-up)"),
    ],
    "50000.023152/2018-96": [
        ('EMPENHO', '2018NE000179-ORIGINAL', "2018NE000179 — complemento até o valor ORIGINAL do empenho (item original R$ 4.000.000,00; o 'NE - Valor' do arquivo, R$ 3.658.661,20, é o saldo na data da extração)", '2018-11-20', 341338.8, "Tesouro Gerencial — 'RADAR - NE ate 2020 - Ano 2016 a 2019.csv', coluna 'NE Item - Valor Total' (espécie ORIGINAL); regra aprovada pelo usuário (04/10/2026, check-up)"),
    ],
    "50000.023721/2020-18": [
        ('EMPENHO', '2020NE000062-ORIGINAL', "2020NE000062 — complemento até o valor ORIGINAL do empenho (item original R$ 600.000,00; o 'NE - Valor' do arquivo, R$ 35.000,00, é o saldo na data da extração)", '2020-09-18', 565000.0, "Tesouro Gerencial — 'RADAR - NE ate 2020 - Ano 2020.csv', coluna 'NE Item - Valor Total' (espécie ORIGINAL); regra aprovada pelo usuário (04/10/2026, check-up)"),
    ],
    "50000.025977/2017-64": [
        ('EMPENHO', '2019NE000113-ORIGINAL', "2019NE000113 — complemento até o valor ORIGINAL do empenho (item original R$ 2.921.245,00; o 'NE - Valor' do arquivo, R$ 1.566.320,31, é o saldo na data da extração)", '2019-10-29', 1354924.69, "Tesouro Gerencial — 'RADAR - NE ate 2020 - Ano 2016 a 2019.csv', coluna 'NE Item - Valor Total' (espécie ORIGINAL); regra aprovada pelo usuário (04/10/2026, check-up)"),
    ],
    "50000.032188/2018-61": [
        ('EMPENHO', '2019NE801917-ORIGINAL', "2019NE801917 — complemento até o valor ORIGINAL do empenho (item original R$ 1.750.000,00; o 'NE - Valor' do arquivo, R$ 1.127.192,82, é o saldo na data da extração)", '2019-12-27', 622807.18, "Tesouro Gerencial — 'RADAR - NE ate 2020 - Ano 2016 a 2019.csv', coluna 'NE Item - Valor Total' (espécie ORIGINAL); regra aprovada pelo usuário (04/10/2026, check-up)"),
    ],
    "50000.035408/2017-27": [
        ('EMPENHO', '2018NE000201-ORIGINAL', "2018NE000201 — complemento até o valor ORIGINAL do empenho (item original R$ 5.100.000,00; o 'NE - Valor' do arquivo, R$ 412.062,12, é o saldo na data da extração)", '2018-11-27', 4687937.88, "Tesouro Gerencial — 'RADAR - NE ate 2020 - Ano 2016 a 2019.csv', coluna 'NE Item - Valor Total' (espécie ORIGINAL); regra aprovada pelo usuário (04/10/2026, check-up)"),
    ],
    "50000.039923/2017-86": [
        ('EMPENHO', '2018NE000325-ORIGINAL', "2018NE000325 — complemento até o valor ORIGINAL do empenho (item original R$ 1.254.009,00; o 'NE - Valor' do arquivo, R$ 1.070.110,13, é o saldo na data da extração)", '2018-12-28', 183898.87, "Tesouro Gerencial — 'RADAR - NE ate 2020 - Ano 2016 a 2019.csv', coluna 'NE Item - Valor Total' (espécie ORIGINAL); regra aprovada pelo usuário (04/10/2026, check-up)"),
    ],
    "50000.040574/2019-15": [
        ('EMPENHO', '2020NE000086-ORIGINAL', "2020NE000086 — complemento até o valor ORIGINAL do empenho (item original R$ 60.000,00; o 'NE - Valor' do arquivo, R$ 23.600,00, é o saldo na data da extração)", '2020-10-27', 36400.0, "Tesouro Gerencial — 'RADAR - NE ate 2020 - Ano 2020.csv', coluna 'NE Item - Valor Total' (espécie ORIGINAL); regra aprovada pelo usuário (04/10/2026, check-up)"),
    ],
    "50000.041740/2019-92": [
        ('EMPENHO', '2019NE000109-ORIGINAL', "2019NE000109 — complemento até o valor ORIGINAL do empenho (item original R$ 3.000.000,00; o 'NE - Valor' do arquivo, R$ 1.494.627,85, é o saldo na data da extração)", '2019-10-24', 1505372.15, "Tesouro Gerencial — 'RADAR - NE ate 2020 - Ano 2016 a 2019.csv', coluna 'NE Item - Valor Total' (espécie ORIGINAL); regra aprovada pelo usuário (04/10/2026, check-up)"),
    ],
    "50000.042503/2019-49": [
        ('EMPENHO', '2019NE000101-ORIGINAL', "2019NE000101 — complemento até o valor ORIGINAL do empenho (item original R$ 1.000.000,00; o 'NE - Valor' do arquivo, R$ 822.832,14, é o saldo na data da extração)", '2019-09-30', 177167.86, "Tesouro Gerencial — 'RADAR - NE ate 2020 - Ano 2016 a 2019.csv', coluna 'NE Item - Valor Total' (espécie ORIGINAL); regra aprovada pelo usuário (04/10/2026, check-up)"),
    ],
    "50000.049439/2017-65": [
        ('EMPENHO', '2020NE801275-ORIGINAL', "2020NE801275 — complemento até o valor ORIGINAL do empenho (item original R$ 5.820.000,00; o 'NE - Valor' do arquivo, R$ 2.566.565,00, é o saldo na data da extração)", '2020-08-05', 3253435.0, "Tesouro Gerencial — 'RADAR - NE ate 2020 - Ano 2020.csv', coluna 'NE Item - Valor Total' (espécie ORIGINAL); regra aprovada pelo usuário (04/10/2026, check-up)"),
    ],
    "50000.060801/2019-11": [
        ('EMPENHO', '2020NE000007-ORIGINAL', "2020NE000007 — complemento até o valor ORIGINAL do empenho (item original R$ 500.000,00; o 'NE - Valor' do arquivo, R$ 25.000,00, é o saldo na data da extração)", '2020-04-28', 475000.0, "Tesouro Gerencial — 'RADAR - NE ate 2020 - Ano 2020.csv', coluna 'NE Item - Valor Total' (espécie ORIGINAL); regra aprovada pelo usuário (04/10/2026, check-up)"),
    ],
}
for _p, _l in NES_VALOR_ORIGINAL.items():
    MOVIMENTOS_DOCUMENTO.setdefault(_p, []).extend(_l)


def _movimentos_documento(processo: str, etapa: str, ja: set) -> list[dict]:
    """Linhas de MOVIMENTOS_DOCUMENTO da etapa que ainda não estão no livro (chave + movimento)."""
    linhas = []
    for et, chave, doc, data, valor, fonte in MOVIMENTOS_DOCUMENTO.get(processo, []):
        if et != etapa or (chave, valor < 0) in ja:
            continue
        linhas.append(_linha(doc, data, valor, valor < 0, "Documento — " + fonte, chave))
    return linhas


def _tem(con: sqlite3.Connection, tabela: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE name=?", (tabela,)).fetchone() is not None


def _num(x) -> float:
    """Valor do SICONV vem como texto BR ('3869130,82'); o do TG já é REAL."""
    t = str(x if x is not None else "0").strip()
    try:
        return float(t.replace(".", "").replace(",", ".")) if "," in t else float(t or 0)
    except ValueError:
        return 0.0


def _iso(d) -> str | None:
    t = str(d or "")
    if "/" in t:
        p = t[:10].split("/")
        return f"{p[2]}-{p[1]}-{p[0]}" if len(p) == 3 else None
    return t[:10] or None


def _linha(documento, data, valor, saida, fonte, chave, considerado=True, situacao=None) -> dict:
    v = abs(float(valor or 0))
    return {"documento": documento, "data": data, "movimento": "Saída" if saida else "Entrada",
            "valor": round(-v if saida else v, 2), "considerado": considerado,
            "situacao": situacao or ("Considerado" if considerado else "Não utilizado"),
            "fonte": fonte, "_chave": chave}


def _marcar_nao_utilizados(linhas: list[dict], processo: str, etapa: str) -> None:
    for l in linhas:
        nota = DOCUMENTOS_ANOTADOS.get((processo, etapa, l["_chave"]))
        if nota and l["considerado"]:
            l["situacao"] = f"{l['situacao']} — {nota}"      # mantém "Considerado" / "Anulada/cancelada — saldo zero"
        motivo = DOCUMENTOS_NAO_UTILIZADOS.get((processo, etapa, l["_chave"]))
        if motivo:
            l["considerado"], l["situacao"] = False, "Não utilizado — " + motivo


def total(linhas: list[dict]) -> float:
    return round(sum(l["valor"] for l in linhas if l["considerado"]), 2)


def _br(iso: str) -> str:
    return f"{iso[8:10]}/{iso[5:7]}/{iso[:4]}" if iso and len(iso) >= 10 else (iso or "")


def _conferir_sequencia_ob(linhas: list[dict]) -> None:
    """O Tesouro Gerencial (radar_ob.csv) não traz data da OB, mas o número é sequencial por UG e ano.
    1) VALIDAÇÃO: OB casada com o SICONV (que tem data) — ordenadas pelo nº da OB, as datas têm de
       crescer; par que quebra a sequência é marcado "Conferir" (casamento por valor suspeito).
    2) DATA ESTIMADA: OB só do TG ganha a janela entre as vizinhas casadas da mesma UG/ano
       (ex. 450907...2026OB000002 ≈ entre 10/02/2026 e 18/02/2026). Teste de 24/09/2026: 23 pares,
       0 inversões, 0 valores ambíguos."""
    por_seq: dict[str, list] = {}
    for l in linhas:
        ob = l.get("_ob_tg")
        if not ob:
            continue
        m = re.match(r"(\d{6})\d{5}(\d{4})OB(\d+)", ob)
        if m:
            por_seq.setdefault(m.group(1) + m.group(2), []).append((int(m.group(3)), l))
    for seq in por_seq.values():
        seq.sort(key=lambda x: x[0])
        for n, l in seq:
            l["_seq"] = n
        ancoras = [(n, l) for n, l in seq if l.get("_data_real")]
        ultima = ""
        for n, l in ancoras:
            if l["_data_real"] < ultima:
                l["situacao"] = "Conferir — data fora da sequência das OBs (casamento por valor suspeito)"
            ultima = max(ultima, l["_data_real"])
        for n, l in seq:
            if l.get("_data_real"):
                continue
            antes = [a for a in ancoras if a[0] < n]
            depois = [a for a in ancoras if a[0] > n]
            ini = antes[-1][1]["_data_real"] if antes else None
            fim = depois[0][1]["_data_real"] if depois else None
            if ini and fim:
                l["data"] = f"≈ entre {_br(ini)} e {_br(fim)}"
            elif ini:
                l["data"] = f"≈ após {_br(ini)}"
            elif fim:
                l["data"] = f"≈ antes de {_br(fim)}"
            l["_ordem"] = ini or fim or "9999"


def _ordem(l: dict) -> tuple:
    """Data real (ou início da janela estimada) e, no empate, o nº da OB — a OB estimada fica logo
    depois da OB casada que a antecede."""
    return (l.get("_ordem") or l.get("data") or "9999", l.get("_seq", 0))


# ------------------------------------------------------------------------------------ EMPENHO
def livro_empenho(con: sqlite3.Connection, processo: str, siafi: str | None, outras_ug: bool = False) -> list[dict]:
    """outras_ug=True (só CONTRATO): inclui o saldo das NEs de outra UG com o processo do contrato — o FNAC descentraliza o
    crédito e a UG executora empenha (ex. SERPRO: UG 390096). Em TC/TED ficam de fora (seria gasto do parceiro)."""
    linhas: list[dict] = []
    grupos: dict[str, list[dict]] = {}
    if _tem(con, "tg_execucao_join"):
        for r in con.execute("SELECT documento, valor, data, metodo_match FROM tg_execucao_join WHERE processo_sei=? "
                             "AND etapa='EMPENHO' AND valor IS NOT NULL ORDER BY COALESCE(data,'9999'), documento", (processo,)):
            doc = r[0] or ""
            m = RE_NE.search(doc)
            chave = m.group(1) if m else doc
            l = _linha(doc, r[2], r[1], (r[3] or "").endswith("_ANULACAO"), "Tesouro Gerencial", chave)
            if abs(l["valor"]) < 0.005 and "NAO SE APLICA" in doc:
                l["considerado"], l["situacao"] = False, "Não utilizado — linha de cabeçalho da NE no Tesouro Gerencial (valor 0,00)"
                linhas.append(l)
                continue
            grupos.setdefault(chave, []).append(l)
    for chave, ls in grupos.items():
        liquido = sum(l["valor"] for l in ls)
        if liquido < -0.005:
            for l in ls:
                l["considerado"] = False
                l["situacao"] = "Não utilizado — saída sem a entrada correspondente nesta base (NE original não vinculada a este processo)"
        elif abs(liquido) <= 0.005 and any(l["movimento"] == "Saída" for l in ls):
            for l in ls:
                l["situacao"] = "Anulada/cancelada — saldo zero"
        linhas += ls
    vistas = set(grupos)

    saldo_ne = {}
    if _tem(con, "tg_execucao_join"):
        for doc, valor, data in con.execute("SELECT documento, valor, data FROM tg_execucao_join WHERE etapa='SALDO_NE' "
                                            "AND documento LIKE ? AND valor > 0.005", (UG_FNAC + "%",)):
            m = RE_NE.search(doc or "")
            if m:
                saldo_ne.setdefault(m.group(1), (float(valor), data, doc))

    # NE do SICONV que já aparece como EVENTO dentro de uma NE do TG (ex. reforço 2018NE800153 da 2018NE800142, Linhares): é o mesmo
    # documento — não entra de novo (02/10/2026)
    eventos_tg = {m.group(1) for ls in grupos.values() for l in ls for m in RE_NE.finditer(l.get("documento") or "")}
    if siafi and _tem(con, "transferegov_siconv_empenho"):
        for nr, valor, data, ug in con.execute("SELECT nr_empenho, valor_empenho, data_emissao, ug_emitente FROM transferegov_siconv_empenho "
                                               "WHERE trim(nr_convenio)=?", (str(siafi).strip(),)):
            m = RE_NE.search(nr or "")
            chave = m.group(1) if m else nr
            if chave in vistas or chave in eventos_tg:
                continue
            vistas.add(chave)
            v, fonte = _num(valor), "SICONV"
            if (ug or UG_FNAC) == UG_FNAC and chave in saldo_ne:
                v, fonte = saldo_ne[chave][0], "SICONV (valor = saldo empenhado do Tesouro Gerencial)"
            if v < -0.005:            # nota de ANULAÇÃO do SICONV (valor negativo): saída (ex. 2012NE800043, conv. 775634 — check-up 04/10/2026)
                linhas.append(_linha(nr, _iso(data), v, True, fonte + " (anulação de empenho)", chave))
                continue
            if v <= 0.005:
                linhas.append(_linha(nr, _iso(data), 0, False, fonte, chave, False, "Não utilizado — NE com valor zero no SICONV"))
                continue
            linhas.append(_linha(nr, _iso(data), v, False, fonte, chave))

    if _tem(con, "tg_execucao_join"):
        for doc, valor, data in con.execute("SELECT documento, valor, data FROM tg_execucao_join WHERE processo_sei=? AND etapa='SALDO_NE' "
                                            "AND (documento LIKE ? OR ?) AND valor > 0.005 ORDER BY data", (processo, UG_FNAC + "%", outras_ug)):
            m = RE_NE.search(doc or "")
            if not m or m.group(1) in vistas:
                continue
            vistas.add(m.group(1))
            ug = (doc or "")[:6]
            fonte = ("Tesouro Gerencial — saldo da NE (complemento)" if ug == UG_FNAC else
                     f"Tesouro Gerencial — saldo atual da NE da UG {ug} (crédito descentralizado pelo FNAC; eventos da NE não estão nas bases)")
            linhas.append(_linha((doc or "")[:m.end()], data, valor, False, fonte, m.group(1)))

    linhas += _movimentos_documento(processo, "EMPENHO", {(l["_chave"], l["movimento"] == "Saída") for l in linhas})
    if _tem(con, "tc_empenhos_manuais"):
        # empenho acrescentado pelo usuário no Cronograma (botão "Adicionar empenho"), com lixeira para excluir
        tem_base = "base" in {r[1] for r in con.execute("PRAGMA table_info(tc_empenhos_manuais)")}
        for mid, doc, chave, valor, data, obs, criado in con.execute(
                "SELECT id, documento, chave, valor, data, observacao, criado_em FROM tc_empenhos_manuais WHERE processo_sei=? "
                + ("AND COALESCE(base, 'empenho')='empenho' " if tem_base else "") + "ORDER BY id", (processo,)):
            l = _linha(doc, data, valor, valor < 0, f"Manual — adicionado pelo usuário em {_br(str(criado or '')[:10])}", chave or doc)
            l["situacao"] = "Considerado — empenho adicionado manualmente" + (f" ({obs})" if obs else "")
            l["_manual_id"] = mid
            linhas.append(l)
    _marcar_nao_utilizados(linhas, processo, "EMPENHO")
    linhas.sort(key=lambda l: (l["_chave"] or "", l["data"] or "9999", l["movimento"]))
    return linhas


def empenhos_liquidos_por_ne(linhas: list[dict]) -> list[dict]:
    """Uma entrada por NE com o saldo líquido considerado (> 0) — para Gantt/Cronograma/card."""
    por_ne: dict[str, dict] = {}
    for l in linhas:
        if not l["considerado"]:
            continue
        d = por_ne.setdefault(l["_chave"], {"documento": l["documento"].split(" (")[0], "valor": 0.0, "data": None,
                                            "_chave_ne": l["_chave"], "fonte": l["fonte"]})
        if l.get("_manual_id"):
            d["_manual_id"] = l["_manual_id"]
        d["valor"] += l["valor"]
        if l["movimento"] == "Entrada" and l["data"] and (d["data"] is None or l["data"] < d["data"]):
            d["data"] = l["data"]
    saida = [d for d in por_ne.values() if d["valor"] > 0.005]
    for d in saida:
        d["valor"] = round(d["valor"], 2)
    return sorted(saida, key=lambda d: (d["data"] or "9999", d["documento"]))


# --------------------------------------------------------------------------------- DESEMBOLSO
def livro_desembolso(con: sqlite3.Connection, processo: str, siafi: str | None, todas_ug: bool = False) -> list[dict]:
    """Recurso ao EXECUTOR. todas_ug=True (CONTRATO): OB ao contratado de qualquer UG (FNAC ou UG executora do crédito descentralizado)."""
    linhas, vistas = [], set()
    if _tem(con, "tg_execucao_join"):
        for doc, valor, data, metodo in con.execute("SELECT documento, valor, data, metodo_match FROM tg_execucao_join WHERE processo_sei=? "
                                                    "AND etapa='PAGAMENTO' AND documento LIKE ? AND valor IS NOT NULL",
                                                    (processo, "%" if todas_ug else UG_FNAC + "%")):
            m = RE_OB.search(doc or "")
            chave = m.group(1) if m else doc
            metodo = metodo or ""
            if metodo.endswith("_NAO_PAGAMENTO"):
                linhas.append(_linha(doc, data, valor, False, "Tesouro Gerencial", chave, False, "Não utilizado — aplicação financeira, não é repasse"))
                continue
            l = _linha(doc, data, valor, metodo.endswith("_ANULACAO"), "Tesouro Gerencial", chave)
            l["_ob_tg"] = doc.split(" (")[0]
            linhas.append(l)
            vistas.add(chave)
    if siafi and _tem(con, "transferegov_siconv_desembolso"):
        for nr, valor, data, obs in con.execute("SELECT nr_siafi, vl_desembolsado, data_desembolso, observacao_dh FROM transferegov_siconv_desembolso "
                                                "WHERE trim(nr_convenio)=? ORDER BY data_desembolso", (str(siafi).strip(),)):
            m = RE_OB.search(nr or "")
            chave = m.group(1) if m else nr
            if chave in vistas:   # mesma OB nas duas fontes: uma linha só, com a data do SICONV
                for l in linhas:
                    if l["_chave"] == chave and not l["data"]:
                        l["data"] = l["_data_real"] = _iso(data)
                        l["fonte"] = "Tesouro Gerencial + SICONV"
                continue
            linhas.append(_linha(nr, _iso(data), _num(valor), False, "SICONV", chave))
    anos = {str(l.get("data") or "")[:4] for l in linhas}
    for data, valor, parcela, cginv, ob, bloco in dinv_parcelas(con).get(processo, []):
        if data[:4] < DINV_ANO_LIMITE and data[:4] not in anos:
            linhas.append(_linha(f"{parcela} — {bloco} (SEI CGINV {cginv}; SEI da OB {ob or '—'})", data, valor, False,
                                 "DINV (planilha financeira, linha 'D' — desembolso anterior a 2023 sem OB nas bases)", f"DINV-{ob or data}-{valor:.2f}"))
    grus = livro_gru(con, processo)
    linhas += grus
    ja = {(l["_chave"], l["movimento"] == "Saída") for l in linhas}
    valores_gru = {round(abs(l["valor"]), 2) for l in grus}
    # GRU digitada de documento (ex. Barreirinhas) sai se a mesma devolução (mesmo valor) já veio do Tesouro Gerencial
    linhas += [l for l in _movimentos_documento(processo, "DESEMBOLSO", ja)
               if not (str(l["_chave"]).startswith("GRU") and round(abs(l["valor"]), 2) in valores_gru)]
    _conferir_sequencia_ob(linhas)
    _marcar_nao_utilizados(linhas, processo, "DESEMBOLSO")
    linhas.sort(key=lambda l: (*_ordem(l), l["_chave"] or ""))
    return linhas


# Parcelas desembolsadas da aba FINANCEIRO do DINV (linhas "D": parcela, data, SEI CGINV, SEI da ordem bancária) — fonte COMPLEMENTAR de
# DESEMBOLSO para os anos sem OB nas bases (o Tesouro Gerencial do RADAR só traz OBs de 2023 em diante): entram só parcelas ANTERIORES
# a 2023 e só se o instrumento não tiver nenhum desembolso no mesmo ano (decisão do usuário, 04/10/2026, check-up). Bloco -> processo
# pelo nome do empreendimento nas abas do DINV; blocos ambíguos ficam de fora (Caruaru, Cascavel TPS 2ª etapa) ou vão por DINV_BLOCOS_PROCESSO.
DINV_BLOCOS_PROCESSO = {"Aeroporto do Guarujá - PPD": "50000.015011/2022-86", "Aeroporto do Guarujá - TPS": "50000.010735/2021-52",
                        "Aeroporto do Guarujá - Cerca": "50000.010735/2021-52"}
DINV_ANO_LIMITE = "2023"
_DINV_CACHE: dict = {}


def dinv_parcelas(con: sqlite3.Connection) -> dict:
    """{processo: [(data ISO, valor, parcela, sei_cginv, sei_ob, bloco)]} — sem repetir a mesma parcela (mesmo SEI da OB e valor) em dois blocos."""
    import json
    if not _tem(con, "dinv_registros"):
        return {}
    chave_cache = con.execute("SELECT COUNT(*), MAX(id) FROM dinv_registros").fetchone()
    if _DINV_CACHE.get("k") == chave_cache:
        return _DINV_CACHE["v"]
    emp: dict = {}
    for (j,) in con.execute("SELECT dados_json FROM dinv_registros WHERE aba IN ('EMPREENDIMENTOS','EMPREENDIMENTOS INFRAERO','DADOS_SIMPLES','INTERFACEWORD')"):
        d = json.loads(j)
        e, p = str(d.get("Empreendimento") or "").strip(), str(d.get("Processo SEI") or "").strip()
        if e and p:
            emp.setdefault(e, set()).add(p)
    inst = {r[0] for r in con.execute("SELECT processo_sei FROM instrumentos")}
    out: dict = {}
    vistos = set()
    bloco = proc = None
    for (j,) in con.execute("SELECT dados_json FROM dinv_registros WHERE aba='FINANCEIRO' ORDER BY linha_excel"):
        d = json.loads(j)
        t = str(d.get("0") or "")
        if (t.startswith("Aeroporto") or t.startswith("Aeródromo")) and d.get("740886.95") not in (None, ""):
            bloco = t
            cand = {DINV_BLOCOS_PROCESSO[t]} if t in DINV_BLOCOS_PROCESSO else emp.get(t, set())
            if not cand and " - " in t:
                base = t.split(" - ")[1]
                cand = {p for e, ps in emp.items() for p in ps if base in e}
            cand = {p for p in cand if p in inst}
            proc = next(iter(cand)) if len(cand) == 1 else None
            continue
        if not proc or d.get("D") != "D":
            continue
        data = str(d.get("2022-08-08T00:00:00") or "")[:10]
        try:
            valor = float(d.get("740886.95") or 0)
        except (TypeError, ValueError):
            continue
        if not re.match(r"\d{4}-\d{2}-\d{2}$", data) or valor <= 0:
            continue
        ob = str(d.get("6032546 / 6675866") or "").strip()
        k = (proc, ob or data, round(valor, 2))
        if k in vistos:
            continue
        vistos.add(k)
        out.setdefault(proc, []).append((data, valor, str(d.get("1a Parcela") or ""), str(d.get("5953139") or ""), ob, bloco))
    _DINV_CACHE.update(k=chave_cache, v=out)
    return out


def livro_gru(con: sqlite3.Connection, processo: str) -> list[dict]:
    """GRU recolhida ao FNAC ligada ao instrumento (radar_gru.csv -> tg_execucao_join etapa GRU). Devolução = saída do
    DESEMBOLSO; rendimento de aplicação financeira = listado, fora do saldo (não é repasse — como na NT 4454643)."""
    if not _tem(con, "tg_execucao_join"):
        return []
    linhas = []
    for doc, valor, data, metodo in con.execute("SELECT documento, valor, data, metodo_match FROM tg_execucao_join WHERE processo_sei=? "
                                                "AND etapa='GRU' AND valor IS NOT NULL ORDER BY data", (processo,)):
        chave = (doc or "").split(" (")[0]
        if (metodo or "").endswith("_RENDIMENTO"):
            linhas.append(_linha(doc, data, valor, True, "Tesouro Gerencial (GRU)", chave, False,
                                 "Não utilizado — devolução de rendimento de aplicação financeira, não é repasse"))
        else:
            linhas.append(_linha(doc, data, valor, True, "Tesouro Gerencial (GRU)", chave))
    return linhas


# ---------------------------------------------------------------------------------- PAGAMENTO
def livro_pagamento(con: sqlite3.Connection, processo: str, siafi: str | None) -> list[dict]:
    """Pagamentos do EXECUTOR ao particular (OBTV/pagamento a favorecido): SICONV + Tesouro Gerencial,
    COMPLEMENTARES e sem repetição (regra do usuário, 24/09/2026 — procedimento padrão).
    O SICONV não guarda o nº da OB, então a mesma OBTV é reconhecida por VALOR no mesmo ANO
    (casamento 1 a 1): primeiro pelo total da OB do TG (ela pode vir dividida por fonte, ex.
    450907...2026OB000027 = 26.275,99 + 28.108,91), depois linha a linha. Ex.: TG
    450907...2026OB000001 R$ 346.190,97 = SICONV 10160559 de 10/02/2026."""
    linhas = []
    sc = []
    if siafi and _tem(con, "transferegov_siconv_pagamento"):
        for nr, valor, data, forn, tipo, desc in con.execute(
                "SELECT nr_mov_fin, vl_pago, data_pag, nome_fornecedor, tp_mov_financeira, desc_dl FROM transferegov_siconv_pagamento "
                "WHERE trim(nr_convenio)=? ORDER BY data_pag", (str(siafi).strip(),)):
            l = _linha(f"{nr} — {forn or ''}".strip(" —"), _iso(data), _num(valor), False, "SICONV", str(nr))
            l["descricao"] = " · ".join(x for x in (tipo, desc) if x)
            sc.append(l)
    livres = list(sc)

    def casar(valor: float, ano: str) -> dict | None:
        for l in livres:
            if abs(l["valor"] - valor) < 0.005 and (l["data"] or "")[:4] == ano:
                livres.remove(l)
                return l
        return None

    tg: dict[str, list] = {}
    if _tem(con, "tg_execucao_join"):
        for doc, valor, metodo in con.execute("SELECT documento, valor, metodo_match FROM tg_execucao_join WHERE processo_sei=? "
                                              "AND etapa='PAGAMENTO' AND documento NOT LIKE ? AND valor IS NOT NULL ORDER BY documento",
                                              (processo, UG_FNAC + "%")):
            m = RE_OB.search(doc or "")
            tg.setdefault(doc.split(" (")[0], []).append((doc, float(valor), metodo or "", m.group(1)[:4] if m else ""))
    for ob, itens in tg.items():
        ano = itens[0][3]
        if all(i[2].endswith("_NAO_PAGAMENTO") for i in itens):
            for doc, valor, _, _ in itens:
                linhas.append(_linha(doc, None, valor, False, "Tesouro Gerencial", ob, False, "Não utilizado — aplicação financeira, não é pagamento"))
            continue
        itens = [i for i in itens if not i[2].endswith("_NAO_PAGAMENTO")]
        saida = any(i[2].endswith("_ANULACAO") for i in itens)
        par = None if saida else casar(sum(i[1] for i in itens), ano)
        if par:
            par["fonte"] = "SICONV + Tesouro Gerencial"
            par["documento"] += f" · OB {ob}"
            par["_ob_tg"], par["_data_real"] = ob, par["data"]
            continue
        for doc, valor, metodo, _ in itens:
            p2 = None if metodo.endswith("_ANULACAO") else casar(valor, ano)
            if p2:
                p2["fonte"] = "SICONV + Tesouro Gerencial"
                p2["documento"] += f" · OB {ob}"
                p2["_ob_tg"], p2["_data_real"] = ob, p2["data"]
            else:
                l = _linha(doc, None, valor, metodo.endswith("_ANULACAO"), "Tesouro Gerencial", ob)
                l["_ob_tg"] = ob
                linhas.append(l)
    linhas += sc
    _conferir_sequencia_ob(linhas)
    _marcar_nao_utilizados(linhas, processo, "PAGAMENTO")
    linhas.sort(key=_ordem)
    return linhas

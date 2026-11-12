"""A synthetic Italian corpus for evals/library.py: five documents of the kinds the library is for (a car policy, a
rental contract, a condominium statement, an ETF KID, a boiler manual), with overlapping topics on purpose (three
of them have a withdrawal clause, two talk about heating costs), and questions labelled with the page that answers
them.

Kinds: `exact` (a code, a plate, an amount: words should win), `paraphrase` (the question uses other words than
the document: 'disdire' for 'recesso', 'cane' for 'animali domestici'), `cross` (which document says it). Written
by the same hand as the questions, so it flatters paraphrase less than real documents will: the decision belongs to
a run on the user's own files (`--docs`).
"""

DOCS: dict[str, list[str]] = {
    "Polizza auto Generali": [
        """GENERALI ITALIA S.P.A. - POLIZZA DI ASSICURAZIONE AUTO
Polizza n. 4401-778213 - Contraente: Marco Rossi

CONDIZIONI GENERALI DI ASSICURAZIONE

Art. 1 - Oggetto dell'assicurazione
La Società assicura, in conformità alle norme di legge, i rischi della responsabilità civile per i quali è
obbligatoria l'assicurazione, impegnandosi a corrispondere le somme dovute a titolo di risarcimento per danni
involontariamente cagionati a terzi dalla circolazione del veicolo targato GH 512 KL.

Art. 2 - Massimali
I massimali di garanzia sono pari a 7.290.000 euro per sinistro per i danni alle persone e 1.220.000 euro per
sinistro per i danni alle cose, indipendentemente dal numero delle vittime.

Art. 3 - Durata e scadenza
Il contratto ha durata annuale con scadenza il 15 marzo di ogni anno e non prevede il tacito rinnovo. La garanzia
resta operante fino alle ore 24 del quindicesimo giorno successivo alla scadenza.""",
        """Art. 4 - Garanzie accessorie
Su richiesta del Contraente e previo pagamento del relativo premio, l'assicurazione è estesa a:
a) Incendio e furto del veicolo, totale o parziale, compresi i danni prodotti nell'esecuzione o nel tentativo del
furto;
b) Eventi atmosferici: grandine, tempesta, uragano, trombe d'aria, caduta di alberi;
c) Cristalli: rottura accidentale di parabrezza, lunotto e cristalli laterali, con un limite di 800 euro per anno;
d) Assistenza stradale: traino del veicolo fino all'officina più vicina entro 50 km, 24 ore su 24.

Art. 5 - Franchigie e scoperti
Per la garanzia eventi atmosferici si applica uno scoperto del 10% con il minimo di 250 euro per sinistro.
Per la garanzia furto si applica uno scoperto del 15%, ridotto al 5% se il veicolo è dotato di antifurto
satellitare attivo al momento del sinistro.
La garanzia cristalli è prestata senza franchigia se la riparazione avviene presso un centro convenzionato.""",
        """Art. 6 - Esclusioni
L'assicurazione non è operante:
- se il conducente non è abilitato alla guida a norma delle disposizioni in vigore;
- nel caso di veicolo con targa in prova, se la circolazione avviene senza l'osservanza delle disposizioni vigenti;
- per i danni subiti dal conducente del veicolo responsabile del sinistro;
- per i danni causati da guida in stato di ebbrezza o sotto l'effetto di sostanze stupefacenti, per i quali la
Società eserciterà il diritto di rivalsa.

Art. 7 - Diritto di recesso
Il Contraente può recedere dal contratto dandone comunicazione alla Società mediante lettera raccomandata o PEC
almeno 30 giorni prima della scadenza annuale. In caso di vendita del veicolo il Contraente può chiedere la
risoluzione del contratto e il rimborso della parte di premio pagata e non goduta, al netto delle imposte.

Art. 8 - Denuncia del sinistro
In caso di sinistro il Contraente deve darne avviso alla Società entro 3 giorni da quando ne è venuto a
conoscenza, utilizzando il modulo di constatazione amichevole (CAI). Il numero verde sinistri è 800 607 607.""",
    ],
    "Contratto di locazione via Solferino": [
        """CONTRATTO DI LOCAZIONE AD USO ABITATIVO (Legge 431/1998, art. 2 comma 1)

Il sig. Paolo Bianchi (locatore) concede in locazione al sig. Marco Rossi (conduttore), che accetta, l'unità
immobiliare sita in Milano, via Solferino 14, piano terzo, interno 9, composta da tre vani più servizi, censita al
catasto al foglio 351, particella 120, subalterno 18.

1. DURATA
Il contratto è stipulato per la durata di anni quattro, dal 1 febbraio 2025 al 31 gennaio 2029, e si rinnova
automaticamente per ulteriori quattro anni salvo disdetta del locatore nei casi previsti dall'art. 3 della legge.

2. CANONE
Il canone annuo è convenuto in euro 15.600, da corrispondersi in dodici rate mensili anticipate di euro 1.300
ciascuna, entro il giorno 5 di ogni mese, mediante bonifico sul conto IBAN IT60 X054 2811 1010 0000 0123 456.
Il canone sarà aggiornato ogni anno nella misura del 75% della variazione ISTAT.""",
        """3. DEPOSITO CAUZIONALE
A garanzia delle obbligazioni assunte, il conduttore versa al locatore la somma di euro 3.900, pari a tre
mensilità del canone, non imputabile in conto canoni e produttiva di interessi legali. Il deposito sarà restituito
al termine della locazione, previa verifica dello stato dell'immobile.

4. RECESSO DEL CONDUTTORE
Il conduttore ha facoltà di recedere anticipatamente dal contratto per gravi motivi, dandone avviso al locatore
con lettera raccomandata almeno sei mesi prima della data in cui il recesso deve avere esecuzione.

5. ONERI ACCESSORI
Sono a carico del conduttore le spese di riscaldamento centralizzato, l'acqua, la pulizia delle scale, la luce
delle parti comuni e il compenso del portiere nella misura del 90%. Le spese sono versate in acconto trimestrale di
euro 280, con conguaglio annuale sulla base del rendiconto approvato dall'assemblea.""",
        """6. MANUTENZIONE E RIPARAZIONI
Le riparazioni di piccola manutenzione (rubinetti, serrature, interruttori, tinteggiatura) sono a carico del
conduttore. Le riparazioni straordinarie, compresa la sostituzione della caldaia e degli infissi, restano a carico
del locatore, che dovrà eseguirle entro trenta giorni dalla segnalazione.

7. DIVIETI
È vietato sublocare in tutto o in parte l'immobile senza il consenso scritto del locatore. È consentita la
detenzione di animali domestici di piccola taglia, purché non arrechino danno all'immobile né disturbo ai vicini.
È vietato installare antenne paraboliche sulla facciata.

8. REGISTRAZIONE
Il contratto è registrato presso l'Agenzia delle Entrate con opzione per la cedolare secca; il locatore rinuncia
pertanto all'aggiornamento ISTAT per tutta la durata dell'opzione.""",
    ],
    "Rendiconto condominio 2025": [
        """CONDOMINIO VIA SOLFERINO 14 - MILANO
RENDICONTO CONSUNTIVO ESERCIZIO 01/10/2024 - 30/09/2025
Amministratore: Studio Ferri, via Moscova 3, tel. 02 655 1234

RIEPILOGO GENERALE DELLE SPESE
Voce                                   Importo
Pulizia scale e parti comuni           6.480,00
Energia elettrica parti comuni         2.215,40
Manutenzione ascensore                 3.120,00
Compenso amministratore                4.392,00
Assicurazione fabbricato               1.870,00
Acqua potabile                         5.940,60
Riscaldamento (gas metano)            38.415,20
Spese bancarie                           186,00
TOTALE SPESE ESERCIZIO                62.619,20""",
        """RIPARTIZIONE SPESE DI RISCALDAMENTO
Le spese di riscaldamento sono ripartite per il 30% in base ai millesimi di proprietà (quota fissa) e per il 70%
in base alle letture dei contabilizzatori di calore installati sui singoli radiatori (quota a consumo), secondo la
norma UNI 10200.

Unità   Condòmino        Millesimi  Quota fissa  Unità lette  Quota consumo  Totale riscald.
Int. 1  Galli Anna        42,10       485,20       1.204        1.120,40       1.605,60
Int. 4  Conti Luigi       55,80       643,10       1.880        1.749,30       2.392,40
Int. 9  Rossi Marco       61,30       706,50       1.512        1.406,90       2.113,40
Int. 12 Ferrari Elena     48,90       563,60         610          567,60       1.131,20
Int. 15 Moretti Davide    58,20       670,70       2.035        1.893,50       2.564,20""",
        """SITUAZIONE CONTABILE PER UNITÀ
Unità   Condòmino        Spese totali  Versato    Saldo
Int. 1  Galli Anna         3.640,20   3.500,00    -140,20
Int. 4  Conti Luigi        4.880,90   5.000,00     119,10
Int. 9  Rossi Marco        4.512,60   4.200,00    -312,60
Int. 12 Ferrari Elena      3.010,30   3.010,30       0,00
Int. 15 Moretti Davide     5.104,80   4.800,00    -304,80

I saldi negativi indicano somme a debito da versare entro il 31 dicembre 2025 sul conto corrente condominiale.
I saldi positivi saranno portati in detrazione della prima rata dell'esercizio successivo.

LAVORI STRAORDINARI DELIBERATI
L'assemblea del 12 giugno 2025 ha deliberato il rifacimento del tetto per un importo di euro 84.000, ripartito in
millesimi di proprietà e da versare in quattro rate trimestrali a partire da gennaio 2026.""",
    ],
    "KID iShares Core MSCI World": [
        """DOCUMENTO CONTENENTE LE INFORMAZIONI CHIAVE (KID)
iShares Core MSCI World UCITS ETF USD (Acc) - ISIN IE00B4L5Y983
Ideatore: BlackRock Asset Management Ireland Limited

Scopo: il presente documento fornisce le informazioni chiave relative a questo prodotto d'investimento. Non si
tratta di un documento promozionale.

Cos'è questo prodotto?
Tipo: quota di un fondo indicizzato quotato (ETF) costituito in Irlanda.
Obiettivi: il fondo mira a replicare la performance dell'indice MSCI World, composto da titoli azionari a media e
alta capitalizzazione di 23 paesi sviluppati. I proventi sono reinvestiti (accumulazione).
Investitore al dettaglio cui si intende commercializzare: investitori in grado di sopportare perdite fino
all'importo investito, con un orizzonte di lungo periodo.""",
        """Quali sono i rischi e qual è il potenziale rendimento?
Indicatore sintetico di rischio: 4 su 7, che corrisponde a una classe di rischio media.
Periodo di detenzione raccomandato: 5 anni. Il rischio effettivo può variare in misura significativa in caso di
disinvestimento in una fase iniziale.

Scenari di performance (investimento di 10.000 euro, dopo 5 anni):
Stress: 2.940 euro (rendimento medio annuo -21,7%)
Sfavorevole: 8.620 euro (-2,9%)
Moderato: 15.110 euro (8,6%)
Favorevole: 21.380 euro (16,4%)

Cosa accade se BlackRock non è in grado di corrispondere quanto dovuto?
Le attività del fondo sono detenute separatamente presso il depositario State Street Custodial Services (Ireland)
Limited; in caso di insolvenza dell'ideatore non vi sarebbero perdite per l'investitore.""",
        """Quali sono i costi?
Costi una tantum di ingresso o di uscita: 0%. Il distributore può applicare commissioni proprie.
Costi correnti (commissioni di gestione e altri costi amministrativi): 0,20% del valore dell'investimento all'anno.
Costi di transazione: 0,01% del valore dell'investimento all'anno.
Commissioni di performance: non previste.

Per quanto tempo devo detenerlo? Posso ritirare il capitale prematuramente?
Il periodo di detenzione raccomandato è di 5 anni. Le quote possono essere vendute in qualsiasi giorno di apertura
della borsa senza penali da parte dell'ideatore.

Come presentare reclami?
I reclami possono essere inviati a BlackRock, 12 Throgmorton Avenue, Londra, oppure via email a
reclami@blackrock.example.""",
    ],
    "Manuale caldaia Vaillant ecoTEC": [
        """VAILLANT ecoTEC plus VMW 25 - ISTRUZIONI PER L'USO

1 SICUREZZA
In caso di odore di gas: non azionare interruttori elettrici, non usare il telefono nell'edificio, chiudere il
rubinetto del contatore del gas, aprire porte e finestre e chiamare il pronto intervento gas dall'esterno.
Non ostruire le aperture di ventilazione del locale in cui è installato l'apparecchio.

2 MESSA IN FUNZIONE
Prima della messa in funzione controllare la pressione dell'impianto sul display: con l'impianto freddo il valore
deve essere compreso tra 1,0 e 1,5 bar. Se la pressione è inferiore a 0,8 bar, riempire l'impianto aprendo il
rubinetto di carico fino a raggiungere 1,2 bar, poi richiuderlo.

3 REGOLAZIONE
La temperatura dell'acqua calda sanitaria si regola da 35 a 65 °C. Per il riscaldamento si consiglia una
temperatura di mandata di 55 °C con il termostato ambiente impostato a 20 °C.""",
        """4 CODICI DI ERRORE
F.22  Mancanza acqua: pressione dell'impianto troppo bassa. Ripristinare la pressione (vedi capitolo 2).
F.28  Mancata accensione: il bruciatore non si accende. Verificare che il rubinetto del gas sia aperto, poi
      premere il tasto reset. Se l'errore si ripete, contattare il centro assistenza.
F.29  Spegnimento della fiamma durante il funzionamento. Premere reset; se ricorre, controllare l'alimentazione
      del gas.
F.75  Guasto della pompa o del sensore di pressione. Contattare il centro assistenza.
F.83  Temperatura di mandata non aumenta: aria nell'impianto. Sfiatare i radiatori.

5 MANUTENZIONE
La legge prescrive una manutenzione periodica dell'apparecchio da parte di un tecnico abilitato, di norma una volta
all'anno, e il controllo dei fumi ogni due anni con registrazione sul libretto di impianto. Pulire l'esterno della
caldaia solo con un panno umido, senza solventi.""",
    ],
}

# (question, document, page, kind)
QUESTIONS: list[tuple[str, str, int, str]] = [
    # exact: a code, a plate, an amount, a name
    ("targa GH 512 KL", "Polizza auto Generali", 1, "exact"),
    ("numero verde sinistri", "Polizza auto Generali", 3, "exact"),
    ("cosa significa F.28?", "Manuale caldaia Vaillant ecoTEC", 2, "exact"),
    ("ISIN IE00B4L5Y983 costi correnti", "KID iShares Core MSCI World", 3, "exact"),
    ("IBAN per il canone d'affitto", "Contratto di locazione via Solferino", 1, "exact"),
    ("saldo interno 9 Rossi", "Rendiconto condominio 2025", 3, "exact"),
    ("quanto costa la manutenzione dell'ascensore?", "Rendiconto condominio 2025", 1, "exact"),
    ("UNI 10200", "Rendiconto condominio 2025", 2, "exact"),
    # paraphrase: other words than the document's
    ("entro quando devo disdire l'assicurazione della macchina?", "Polizza auto Generali", 3, "paraphrase"),
    ("se vendo l'auto mi ridanno dei soldi?", "Polizza auto Generali", 3, "paraphrase"),
    ("quanto pago di tasca mia se la grandine mi rovina la carrozzeria?", "Polizza auto Generali", 2, "paraphrase"),
    ("se mi rompono il parabrezza sono coperto?", "Polizza auto Generali", 2, "paraphrase"),
    ("mi coprono se guido ubriaco?", "Polizza auto Generali", 3, "paraphrase"),
    ("fino a quanto risarciscono se ferisco qualcuno?", "Polizza auto Generali", 1, "paraphrase"),
    ("posso tenere un gatto nell'appartamento?", "Contratto di locazione via Solferino", 3, "paraphrase"),
    ("con quanto anticipo devo avvisare il padrone di casa se voglio andarmene?",
     "Contratto di locazione via Solferino", 2, "paraphrase"),
    ("quanto ho lasciato di cauzione?", "Contratto di locazione via Solferino", 2, "paraphrase"),
    ("chi paga se si rompe la caldaia di casa in affitto?", "Contratto di locazione via Solferino", 3, "paraphrase"),
    ("quando scade l'affitto?", "Contratto di locazione via Solferino", 1, "paraphrase"),
    ("posso affittare una stanza a un amico?", "Contratto di locazione via Solferino", 3, "paraphrase"),
    ("come vengono divisi i costi del calore tra gli appartamenti?", "Rendiconto condominio 2025", 2, "paraphrase"),
    ("quanto devo ancora dare al condominio?", "Rendiconto condominio 2025", 3, "paraphrase"),
    ("quanto costerà sistemare la copertura dell'edificio?", "Rendiconto condominio 2025", 3, "paraphrase"),
    ("quanto si spende in totale per il gas del palazzo?", "Rendiconto condominio 2025", 1, "paraphrase"),
    ("quanto è rischioso questo fondo?", "KID iShares Core MSCI World", 2, "paraphrase"),
    ("quanto mi costa all'anno tenerlo?", "KID iShares Core MSCI World", 3, "paraphrase"),
    ("cosa succede ai miei soldi se l'emittente fallisce?", "KID iShares Core MSCI World", 2, "paraphrase"),
    ("nel caso peggiore quanto potrei perdere?", "KID iShares Core MSCI World", 2, "paraphrase"),
    ("i dividendi vengono pagati?", "KID iShares Core MSCI World", 1, "paraphrase"),
    ("la caldaia segna poca pressione, cosa faccio?", "Manuale caldaia Vaillant ecoTEC", 1, "paraphrase"),
    ("i termosifoni restano freddi", "Manuale caldaia Vaillant ecoTEC", 2, "paraphrase"),
    ("ogni quanto va fatto il controllo della caldaia?", "Manuale caldaia Vaillant ecoTEC", 2, "paraphrase"),
    ("sento puzza di gas in cucina", "Manuale caldaia Vaillant ecoTEC", 1, "paraphrase"),
    # cross: which document, when several talk about the same thing
    ("recesso del conduttore", "Contratto di locazione via Solferino", 2, "cross"),
    ("chi paga il riscaldamento nell'appartamento in affitto?", "Contratto di locazione via Solferino", 2, "cross"),
    ("quanto ho speso io di riscaldamento l'anno scorso?", "Rendiconto condominio 2025", 2, "cross"),
    ("la polizza si rinnova da sola?", "Polizza auto Generali", 1, "cross"),
    ("posso vendere le quote quando voglio senza penali?", "KID iShares Core MSCI World", 3, "cross"),
]

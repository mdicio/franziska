"""Who the money went to or came from, read deterministically out of the bank's description text.

The bank writes the counterparty in a handful of fixed shapes (as of the May 2026 export):
- POS, new style: 'EFFETTUATO IL 17/11/2025 ALLE ORE 09:49 MEDIANTE LA CARTA ... PRESSO HOTEL CENTRALE MILANO MILANO'
- POS, old style: 'CONAD SUPERMERCATO VIALE MO21/03-20:01 - Carta n.1234 ...' (name cut at 26 characters)
- Bancomat Pay:   'presso CINEMA ODEON data: 14.02 ore: 22:47 ...'
- Direct debit:   'COD. DISP.: ... NOME: WIND TRE S P A MANDATO: ...'
- Transfer out:   '... Bonifico da Voi disposto a favore di: Condominio Via Po Rata 1 ...'
- Transfer in:    '... Bonifico a Vostro favore disposto da: MITT.: ACME ITALIA S.R.L. BENEF.: ...'

Intesa's 'Lista Operazione' export writes the same fields in title case and drops punctuation ('Nome Wind Tre S P A
Mandato', 'Presso CINEMA ODEON Data 14.02'), so every pattern ignores case and treats ':' as optional. Revolut's text is
the merchant itself, or '<kind> to/from <party>' for transfers.

The result is normalised (upper case, no legal suffix, no per-payment codes) so the same payee groups together,
which recurring-payment detection relies on. Unknown shapes give None: the row still counts, it just has no merchant,
and the import reports it (`recognised`), so a new shape is seen rather than lumped under its first two words
('ADDEBITO DIRETTO' once held both Wind Tre and TIM).

`of` is the one derivation from a row's stored fields: the importers call it, and the ledger re-derives every row with
it when it opens (ledger.connect), so a fix here reaches movements imported before it.
"""

import re

_PATTERNS = [
    re.compile(r"\bPRESSO\s+(?:ABI\b.*|(?P<m>.+?))(?:\s+\(CTV\.|\s+data:?\s+\d|$)", re.IGNORECASE | re.DOTALL),
    # Punctuation optional: Intesa's 'Lista Operazione' export strips it ('23/102035 Carta N.1234', 'MITT. X').
    re.compile(r"^(?P<m>.+?)\d{2}/\d{2}-?\d{2}:?\d{2}\s*-?\s*Carta", re.IGNORECASE),
    re.compile(r"\bNOME:?\s*(?P<m>.+?)\s+MANDATO\b", re.IGNORECASE),
    re.compile(r"a favore di:?\s*(?P<m>.+)$", re.IGNORECASE),
    re.compile(r"\bMITT\.?:?\s*(?P<m>.+?)\s+BENEF\b", re.IGNORECASE),
]
# Revolut transfers: 'To X', 'Transfer from X', 'Payment from X', 'SWIFT transfer to X', 'SEPA transfer to X'.
_REVOLUT_PARTY = re.compile(r"^(?:[A-Z]+\s+)?(?:transfer\s+|payment\s+)?(?:to|from)\s+(?P<m>.+)$", re.IGNORECASE)
_LEGAL = re.compile(r"(?:\s+(?:S\s?P\s?A|S\s?R\s?L|S\s?A\s?S|S\s?N\s?C|S\s?C\s?A\s?R\s?L|SOC\s+COOP))+$")
# A per-payment reference after '*' (Amazon.it*ZG4VN2PO4): upper-case letters and digits mixed. A PayPal payee
# ('PAYPAL *XSOLLA', 'PAYPAL *mariorossi77') is kept.
_REF = re.compile(r"\*(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{6,}\b")
_LONG_NUMBER = re.compile(r"\d{6,}")  # terminal ids, booking codes
MAX_LEN = 40


def extract(description: str, details: str) -> str | None:
    """The counterparty an Intesa row's text names, or None when its shape is not one of _PATTERNS."""
    if description.upper().startswith("PRELIEVO"):
        return "ATM"
    for pattern in _PATTERNS:
        if (m := pattern.search(details or "")) and m.group("m"):
            return normalise(m.group("m"))
    return None


def of(description: str, details: str | None, source_format: str | None, kind: str | None) -> str | None:
    """A bank row's merchant from its stored fields (importer formats: 'intesa_operazioni', 'intesa_movimenti',
    'revolut'). None: the text has no shape we know; categories.payee_of then falls back to the description."""
    if source_format and source_format.startswith("mapped:"):  # mapped.py: the description column is the payee
        return normalise(description)
    if source_format == "revolut":
        if kind == "interest":
            return "REVOLUT INTEREST"
        m = _REVOLUT_PARTY.match(description) if (kind or "").startswith(("transfer", "topup")) else None
        return normalise(m.group("m") if m else description)
    found = extract(description, details or "")
    if source_format == "intesa_operazioni":  # its 'Operazione' column is often the merchant itself
        return found or normalise(description)
    return found


def recognised(description: str, details: str | None, source_format: str | None) -> bool:
    """Whether the counterparty was read from a known shape rather than guessed from the description. Revolut's
    description is the merchant, so always, and so is a mapped bank's description column (mapped.py, the column you
    confirmed); Intesa only through _PATTERNS (or an ATM withdrawal)."""
    if source_format == "revolut" or (source_format or "").startswith("mapped:"):
        return True
    return extract(description, details or "") is not None


def normalise(name: str) -> str | None:
    name = re.sub(r"^PAGAMENTO SU POS\s+", "", name.strip(), flags=re.IGNORECASE)
    name = _LONG_NUMBER.sub(" ", _REF.sub(" ", name))
    name = re.sub(r"[.,'’]", " ", name).upper()
    name = _LEGAL.sub("", " ".join(name.split()))
    return name[:MAX_LEN].strip() or None


def key(merchant: str) -> str:
    """The payee without the branch or city the bank appends ('ROSSI MARIO DI MILANO' and '... PIAZZA DUOMO')."""
    return " ".join(merchant.split()[:2])

from decimal import Decimal, ROUND_HALF_UP


MONEY_QUANT = Decimal("0.00000001")


def to_decimal(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None or value == "":
        return Decimal("0")
    return Decimal(str(value))


def normalize_money(value, quant: Decimal = MONEY_QUANT) -> Decimal:
    return to_decimal(value).quantize(quant, rounding=ROUND_HALF_UP)


def to_storage_float(value, quant: Decimal = MONEY_QUANT) -> float:
    return float(normalize_money(value, quant))


def to_storage_str(value, quant: Decimal = MONEY_QUANT) -> str:
    return format(normalize_money(value, quant), "f")
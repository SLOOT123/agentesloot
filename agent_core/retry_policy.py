"""Modelo de espera por sitio; no transforma un rechazo HTTP en un éxito."""
import math


def backoff_delay(failures, retry_after=None, *, base_seconds=300, cap_seconds=3600):
    """max(Retry-After, min(tope, base * 2**(fallos-1))). Sin reintento inmediato."""
    if type(failures) is not int or failures < 1:
        raise ValueError('failures debe ser un entero positivo')
    if not all(math.isfinite(value) and value > 0 for value in (base_seconds, cap_seconds)):
        raise ValueError('La base y el tope deben ser positivos y finitos')
    try:
        requested = float(retry_after or 0)
    except (TypeError, ValueError, OverflowError):
        requested = 0
    if not math.isfinite(requested) or requested < 0:
        requested = 0
    # Comparar exponentes evita desbordamientos incluso con un contador enorme.
    exponent = failures - 1
    exponential = (cap_seconds if exponent >= math.log2(cap_seconds) - math.log2(base_seconds)
                   else math.ldexp(base_seconds, exponent))
    return max(requested, exponential)

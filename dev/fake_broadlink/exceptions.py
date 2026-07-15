"""Miroir des exceptions de broadlink.exceptions consommées par app.py."""


class BroadlinkException(Exception):
    pass


class ReadError(BroadlinkException):
    """Rien à lire — le vrai appareil la lève tant qu'aucune trame n'est reçue."""


class StorageError(BroadlinkException):
    """Appareil pas en mode apprentissage."""


class NetworkTimeoutError(BroadlinkException):
    """Pas de réponse — l'appareil est absent, ou il dort (§8)."""

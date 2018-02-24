import hashlib
import logging


import cryptography.hazmat.primitives.hashes as c_hashes
import cryptography.hazmat.primitives.asymmetric.ec as c_ec
import cryptography.hazmat.primitives.serialization as c_ser
import cryptography.hazmat.primitives.asymmetric.utils as c_utils

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.backends import default_backend

logger = logging.getLogger(__name__)

class PriKey:
    '''
    A helper class for using ECC private keys.
    '''
    def __init__(self, priv):
        self.priv = priv  # type: c_ec.EllipticCurvePrivateKey
        self.publ = PubKey(self.priv.public_key())

    def iden(self):
        '''
        Return a SHA256 hash for the public key (to be used as a GUID).

        Returns:
            str: The SHA256 hash of the public key bytes.
        '''
        return self.publ.iden()

    def sign(self, byts):
        '''
        Compute the ECC signature for the given bytestream.

        Args:
            byts (bytes): The bytes to sign.

        Returns:
            bytes: The RSA Signature bytes.
        '''
        chosen_hash = c_hashes.SHA256()
        hasher = c_hashes.Hash(chosen_hash, default_backend())
        hasher.update(byts)
        digest = hasher.finalize()
        return self.priv.sign(digest,
                              c_ec.ECDSA(c_utils.Prehashed(chosen_hash))
                              )

    def exchange(self, pubkey):
        '''
        Perform a ECDH key exchange with a public key.

        Args:
            pubkey (PubKey): A PubKey to perform the ECDH with.

        Returns:
            bytes: The ECDH bytes. This is deterministic for a given pubkey
            and private key.
        '''
        return self.priv.exchange(c_ec.ECDH(), pubkey.publ)

    def public(self):
        '''
        Get the PubKey which corresponds to the ECC PriKey.

        Returns:
            PubKey: A new PubKey object whose key corresponds to the private key.
        '''
        return PubKey(self.priv.public_key())

    @staticmethod
    def generate():
        '''
        Generate a new ECC PriKey instance.

        Returns:
            PriKey: A new PriKey instance.
        '''
        return PriKey(c_ec.generate_private_key(
            c_ec.SECP384R1(),
            default_backend()
        ))

    def dump(self):
        '''
        Get the private key bytes in DER/PKCS8 format.

        Returns:
            bytes: The DER/PKCS8 encoded private key.
        '''
        return self.priv.private_bytes(
            encoding=c_ser.Encoding.DER,
            format=c_ser.PrivateFormat.PKCS8,
            encryption_algorithm=c_ser.NoEncryption())

    @staticmethod
    def load(byts):
        '''
        Create a PriKey instance from DER/PKCS8 encoded bytes.

        Args:
            byts (bytes): Bytes to load

        Returns:
            PriKey: A new PubKey instance.
        '''
        return PriKey(c_ser.load_der_private_key(
                byts,
                password=None,
                backend=default_backend()))

class PubKey:
    '''
    A helper class for using ECC public keys.
    '''

    def __init__(self, publ):
        self.publ = publ  # type: c_ec.EllipticCurvePublicKey

    def dump(self):
        '''
        Get the public key bytes in DER/SubjectPublicKeyInfo format.

        Returns:
            bytes: The DER/SubjectPublicKeyInfo encoded public key.
        '''
        return self.publ.public_bytes(
            encoding=c_ser.Encoding.DER,
            format=c_ser.PublicFormat.SubjectPublicKeyInfo)

    def verify(self, byts, sign):
        '''
        Verify the signature for the given bytes using the ECC
        public key.

        Args:
            byts (bytes): The data bytes.
            sign (bytes): The signature bytes.

        Returns:
            bool: True if the data was verified, False otherwise.
        '''
        try:
            chosen_hash = c_hashes.SHA256()
            hasher = c_hashes.Hash(chosen_hash, default_backend())
            hasher.update(byts)
            digest = hasher.finalize()
            self.publ.verify(sign,
                             digest,
                             c_ec.ECDSA(c_utils.Prehashed(chosen_hash))
                             )
            return True
        except InvalidSignature as e:
            logger.exception('Error in publ.verify')
            return False

    def iden(self):
        '''
        Return a SHA256 hash for the public key (to be used as a GUID).

        Returns:
            str: The SHA256 hash of the public key bytes.
        '''
        return hashlib.sha256(self.dump()).hexdigest()

    @staticmethod
    def load(byts):
        '''
        Create a PubKey instance from DER/PKCS8 encoded bytes.

        Args:
            byts (bytes): Bytes to load

        Returns:
            PubKey: A new PubKey instance.
        '''
        return PubKey(c_ser.load_der_public_key(
                byts,
                backend=default_backend()))

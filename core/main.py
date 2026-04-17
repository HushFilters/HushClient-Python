import struct
import mmap
import gzip
import random
from pprint import pprint

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.exceptions import InvalidSignature

from core.hash import bloom_probe_hashes, credential_digest, parse_sha256_hex

MMM3_64_DOUBLE = 1

# logging.basicConfig(level=logging.DEBUG, filename="debug.log", filemode="w", 
#                     format="%(asctime)s - %(levelname)s - %(message)s")

# logger = logging.getLogger(__name__)

class HushFilterError(Exception):
    pass


class HushFilter:
    def __init__(self, path: str):
        # Open the file and memory-map it for reading
        self.f = open(path, "rb")
        self.mm = mmap.mmap(self.f.fileno(), 0, access=mmap.ACCESS_READ)
        
        # Parse the header from the memory-mapped file
        self.header = self.parse_header()
        self.public_key = self.decode_public_key(self.header['sauth'])

        # Load filter data location based on header information
        self.load_filter()

    def parse_header(self) -> dict:
        header = {}
        header['magic'] = self.mm[0:4].decode('ascii')
        if header['magic'] != 'HUSH':
            raise ValueError("Invalid Hushfilter file (Magic number mismatch).")

        header['version'] = struct.unpack('>H', self.mm[4:6])[0]
        header['timestamp'] = struct.unpack('>Q', self.mm[6:14])[0]
        header['ftype'] = self.mm[14]
        header['fcontent'] = self.mm[15]
        header['foffset'] = struct.unpack('>I', self.mm[16:20])[0]
        header['fsize'] = struct.unpack('>Q', self.mm[20:28])[0]
        header['fpsize'] = struct.unpack('>H', self.mm[28:30])[0]

        param_offset = 30
        if header['ftype'] == 1:  # Bloom Filter specific
            self.nbits = struct.unpack('>Q', self.mm[param_offset:param_offset + 8])[0]
            self.hash_func = self.mm[param_offset + 8]  # uint8
            self.nk = self.mm[param_offset + 9]  # uint8
            if self.hash_func != MMM3_64_DOUBLE:
                raise HushFilterError(f"Unsupported bloom hash function: {self.hash_func}")

        offset = param_offset + header['fpsize']
        header['sasize'] = struct.unpack('>H', self.mm[offset:offset + 2])[0]
        offset += 2

        if header['sasize'] > 0:
            header['sauth'] = self.mm[offset:offset + header['sasize']]
            offset += header['sasize']
            header['sigtype'] = self.mm[offset]
            header['chunksize'] = self.mm[offset + 1]
            header['sigsize'] = struct.unpack('>H', self.mm[offset + 2:offset + 4])[0]
            header['slsize'] = struct.unpack('>H', self.mm[offset + 4:offset + 6])[0]
            offset += 6
            header['siglist'] = self.mm[offset:offset + header['slsize'] * header['sigsize']]
        else:
            raise HushFilterError("No signature present; parsing terminated.")

        # Suppress verbose output by default
        # pprint(header)
        
        return header

    
    def decode_public_key(self, sauth: bytes):
        """Extract the public key from the sauth field in ASN.1 DER format."""
        public_key = serialization.load_der_public_key(sauth, backend=default_backend())
        # print("Public key decoded successfully.")
        return public_key
    
    def load_filter(self):
        """Set boundaries for filter data based on header."""
        self.filter_start = self.header['foffset']
        self.filter_end = self.header['foffset'] + self.header['fsize']
    
    def chunk_data(self, chunk_size: int) -> list:
        """Divide filter data into chunks of specified size; last chunk may be smaller."""
        chunks = []
        for i in range(self.filter_start, self.filter_end, chunk_size):
            # Calculate remaining bytes for the last chunk
            remaining = self.filter_end - i
            if remaining < chunk_size:
                chunk = self.mm[i:i + remaining]
            else:
                chunk = self.mm[i:i + chunk_size]
            chunks.append(chunk)

        return chunks

    def verify_signature(self, data: bytes, signature: bytes) -> bool:
        try:
            self.public_key.verify(
                signature,
                data,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=32
                ),
                hashes.SHA256()
            )
            return True
        except InvalidSignature:
            print("Signature is invalid.")
            return False
        except (TypeError, ValueError) as e:
            print ("Invalid signature: %s", str(e))
            return False
        except Exception as e:
            print("Unexpected error during signature validation:", e)
            return False
        
    def validate_header(self):
        """Validate the header's first chunk signature using RSA-PSS."""
        header_chunk_size = (30 + self.header['fpsize'] + 2 + self.header['sasize'] + 6 + len(self.header['siglist']))
        header_chunk = self.mm[:header_chunk_size]
        header_signature = self.header['siglist'][:self.header['sigsize']]

        siglist_copy = bytearray(self.header['siglist'])
        siglist_copy[:self.header['sigsize']] = b'\x00' * self.header['sigsize']
        header_chunk_with_zeroed_header_signature = header_chunk[:-len(self.header['siglist'])] + siglist_copy

        # Validate header chunk with zeroed-out signature
        if self.verify_signature(header_chunk_with_zeroed_header_signature, header_signature):
            print("Header signature validated successfully.")
        else:
            print("Header signature validation failed.")
    
    def validate_chunks(self):
        """Validate header and a subset of chunks, skipping unsigned data between header and foffset."""
        chunk_size = 2 ** self.header['chunksize']
     
        chunks = self.chunk_data(chunk_size) 

        num_chunks_to_validate = min(10, len(chunks)) 
        chunks_to_validate = random.sample(range(1, len(chunks) + 1), num_chunks_to_validate) 
        
        for i in chunks_to_validate:
          
            chunk = chunks[i - 1]
            signature = self.header['siglist'][i * self.header['sigsize']:(i + 1) * self.header['sigsize']]

            print(f"Validating signature for chunk {i}.")  # Debug: print current chunk index
            
            if self.verify_signature(chunk, signature):
                print(f"Valid signature for chunk {i}.")
            else:
                raise HushFilterError(f"Invalid signature for chunk {i}.")
    
    def check(self, username: str, password: str) -> bool:
        """
        Check if a username/password combination is in the bloom filter.
        
        Arguments:
            username: The username/account/email to check
            password: The password to check
        
        Returns:
            False: Credentials are NOT in the filter
            True: Credentials are LIKELY in the filter (may have false positives)
        """
        digest = credential_digest(username, password)

        for probe_hash in bloom_probe_hashes(digest, nk=self.nk):
            pos = probe_hash % self.nbits
            
            # Check if the bit at position 'pos' is set
            byte_index = pos // 8
            bit_index = pos % 8
            
            # Read the byte at filter_start + byte_index
            byte_offset = self.filter_start + byte_index
            byte_value = self.mm[byte_offset]
            
            # Check if the specific bit is set
            if (byte_value & (1 << bit_index)) == 0:
                return False
        
        return True

    def check_sha256_hash(self, sha256_hash: str) -> bool:
        """
        Check membership from a precomputed SHA-256 hex digest string.

        Arguments:
            sha256_hash: 64-character hexadecimal SHA-256 credential digest
        """
        digest = parse_sha256_hex(sha256_hash)

        for probe_hash in bloom_probe_hashes(digest, nk=self.nk):
            pos = probe_hash % self.nbits

            byte_index = pos // 8
            bit_index = pos % 8
            byte_offset = self.filter_start + byte_index
            byte_value = self.mm[byte_offset]
            if (byte_value & (1 << bit_index)) == 0:
                return False

        return True
    
def main():
    # Load filter from .hf file
    hush_filter = HushFilter("filters/filter100.hf")
    hush_filter.validate_header()
    hush_filter.validate_chunks()
    output = hush_filter.check(username="NZHKPQBESUQLGQLVI5OIK4B4KC", password="")
    print("Check result:", output)

if __name__ == "__main__":
    main()

// Encryption at rest for event data, refresh/access tokens and subscription secrets.
//
// Fernet (AES-128-CBC + HMAC-SHA256), byte-compatible with Python's
// cryptography.fernet.MultiFernet, so data written by the previous backend still decrypts
// and DATA_ENCRYPTION_KEYS keeps its format: comma-separated keys, the first encrypts and
// all of them decrypt (rotation by prepending a new key).

const VERSION = 0x80;
const HEADER = 1 + 8 + 16; // version, timestamp, IV
const HMAC_LEN = 32;

export class InvalidFernetToken extends Error {}

export function b64urlDecode(value: string): Uint8Array<ArrayBuffer> {
  let s = value.trim().replace(/-/g, "+").replace(/_/g, "/");
  while (s.length % 4) s += "=";
  const bin = atob(s);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

/** URL-safe base64 *with* padding, as Python's base64.urlsafe_b64encode produces. */
export function b64urlEncode(bytes: Uint8Array): string {
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  }
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_");
}

interface FernetKey {
  signing: CryptoKey;
  encryption: CryptoKey;
}

async function importKey(key: string): Promise<FernetKey> {
  const raw = b64urlDecode(key);
  if (raw.length !== 32) throw new Error("Fernet key must be 32 url-safe base64-encoded bytes");
  const [signing, encryption] = await Promise.all([
    crypto.subtle.importKey("raw", raw.slice(0, 16), { name: "HMAC", hash: "SHA-256" }, false, [
      "sign",
      "verify",
    ]),
    crypto.subtle.importKey("raw", raw.slice(16), { name: "AES-CBC" }, false, ["encrypt", "decrypt"]),
  ]);
  return { signing, encryption };
}

export class Cipher {
  private constructor(private readonly keys: FernetKey[]) {}

  static async create(keys: string[]): Promise<Cipher> {
    if (!keys.length) throw new Error("At least one data encryption key is required");
    return new Cipher(await Promise.all(keys.map(importKey)));
  }

  async encrypt(plain: string): Promise<string> {
    const key = this.keys[0];
    const iv = crypto.getRandomValues(new Uint8Array(16));
    const ciphertext = new Uint8Array(
      await crypto.subtle.encrypt({ name: "AES-CBC", iv }, key.encryption, new TextEncoder().encode(plain)),
    );
    const body = new Uint8Array(HEADER + ciphertext.length);
    body[0] = VERSION;
    new DataView(body.buffer).setBigUint64(1, BigInt(Math.floor(Date.now() / 1000)));
    body.set(iv, 9);
    body.set(ciphertext, HEADER);
    const mac = new Uint8Array(await crypto.subtle.sign("HMAC", key.signing, body));
    const token = new Uint8Array(body.length + HMAC_LEN);
    token.set(body);
    token.set(mac, body.length);
    return b64urlEncode(token);
  }

  async decrypt(token: string): Promise<string> {
    let data: Uint8Array<ArrayBuffer>;
    try {
      data = b64urlDecode(token);
    } catch {
      throw new InvalidFernetToken("Token is not base64");
    }
    const ctLength = data.length - HEADER - HMAC_LEN;
    if (data[0] !== VERSION || ctLength <= 0 || ctLength % 16 !== 0) {
      throw new InvalidFernetToken("Malformed Fernet token");
    }
    const body = data.subarray(0, data.length - HMAC_LEN);
    const mac = data.subarray(data.length - HMAC_LEN);
    for (const key of this.keys) {
      if (!(await crypto.subtle.verify("HMAC", key.signing, mac, body))) continue;
      const plain = await crypto.subtle.decrypt(
        { name: "AES-CBC", iv: data.subarray(9, HEADER) },
        key.encryption,
        data.subarray(HEADER, data.length - HMAC_LEN),
      );
      return new TextDecoder().decode(plain);
    }
    throw new InvalidFernetToken("No key could decrypt the token");
  }

  async encryptOptional(value: string | null | undefined): Promise<string | null> {
    return value == null ? null : await this.encrypt(value);
  }

  async decryptOptional(value: string | null | undefined): Promise<string | null> {
    return value == null ? null : await this.decrypt(value);
  }
}

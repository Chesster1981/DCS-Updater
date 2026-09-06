"""Local DCS dedicated-server WebGUI client (getPlayers only).

Talks to 127.0.0.1 on the per-instance WebGUI port (88xx). The dedicated
server always exposes one admin/host slot; that slot is excluded from
counts and name lists.

AES-CBC core adapted from BoppreH aes (MIT): https://github.com/boppreh/aes
Request format matches ActiumDev/dcs-webgui-python (MIT).
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import logging
import secrets
from typing import Any, Optional

logger = logging.getLogger("dcs_webgui")

WEBGUI_KEY = b"DigitalCombatSimulator.com"
WEBGUI_PORT_BASE = 8800

# AES S-box / inverse / rcon (FIPS-197)
_SBOX = (
    0x63, 0x7C, 0x77, 0x7B, 0xF2, 0x6B, 0x6F, 0xC5, 0x30, 0x01, 0x67, 0x2B, 0xFE, 0xD7, 0xAB, 0x76,
    0xCA, 0x82, 0xC9, 0x7D, 0xFA, 0x59, 0x47, 0xF0, 0xAD, 0xD4, 0xA2, 0xAF, 0x9C, 0xA4, 0x72, 0xC0,
    0xB7, 0xFD, 0x93, 0x26, 0x36, 0x3F, 0xF7, 0xCC, 0x34, 0xA5, 0xE5, 0xF1, 0x71, 0xD8, 0x31, 0x15,
    0x04, 0xC7, 0x23, 0xC3, 0x18, 0x96, 0x05, 0x9A, 0x07, 0x12, 0x80, 0xE2, 0xEB, 0x27, 0xB2, 0x75,
    0x09, 0x83, 0x2C, 0x1A, 0x1B, 0x6E, 0x5A, 0xA0, 0x52, 0x3B, 0xD6, 0xB3, 0x29, 0xE3, 0x2F, 0x84,
    0x53, 0xD1, 0x00, 0xED, 0x20, 0xFC, 0xB1, 0x5B, 0x6A, 0xCB, 0xBE, 0x39, 0x4A, 0x4C, 0x58, 0xCF,
    0xD0, 0xEF, 0xAA, 0xFB, 0x43, 0x4D, 0x33, 0x85, 0x45, 0xF9, 0x02, 0x7F, 0x50, 0x3C, 0x9F, 0xA8,
    0x51, 0xA3, 0x40, 0x8F, 0x92, 0x9D, 0x38, 0xF5, 0xBC, 0xB6, 0xDA, 0x21, 0x10, 0xFF, 0xF3, 0xD2,
    0xCD, 0x0C, 0x13, 0xEC, 0x5F, 0x97, 0x44, 0x17, 0xC4, 0xA7, 0x7E, 0x3D, 0x64, 0x5D, 0x19, 0x73,
    0x60, 0x81, 0x4F, 0xDC, 0x22, 0x2A, 0x90, 0x88, 0x46, 0xEE, 0xB8, 0x14, 0xDE, 0x5E, 0x0B, 0xDB,
    0xE0, 0x32, 0x3A, 0x0A, 0x49, 0x06, 0x24, 0x5C, 0xC2, 0xD3, 0xAC, 0x62, 0x91, 0x95, 0xE4, 0x79,
    0xE7, 0xC8, 0x37, 0x6D, 0x8D, 0xD5, 0x4E, 0xA9, 0x6C, 0x56, 0xF4, 0xEA, 0x65, 0x7A, 0xAE, 0x08,
    0xBA, 0x78, 0x25, 0x2E, 0x1C, 0xA6, 0xB4, 0xC6, 0xE8, 0xDD, 0x74, 0x1F, 0x4B, 0xBD, 0x8B, 0x8A,
    0x70, 0x3E, 0xB5, 0x66, 0x48, 0x03, 0xF6, 0x0E, 0x61, 0x35, 0x57, 0xB9, 0x86, 0xC1, 0x1D, 0x9E,
    0xE1, 0xF8, 0x98, 0x11, 0x69, 0xD9, 0x8E, 0x94, 0x9B, 0x1E, 0x87, 0xE9, 0xCE, 0x55, 0x28, 0xDF,
    0x8C, 0xA1, 0x89, 0x0D, 0xBF, 0xE6, 0x42, 0x68, 0x41, 0x99, 0x2D, 0x0F, 0xB0, 0x54, 0xBB, 0x16,
)
_INV_SBOX = (
    0x52, 0x09, 0x6A, 0xD5, 0x30, 0x36, 0xA5, 0x38, 0xBF, 0x40, 0xA3, 0x9E, 0x81, 0xF3, 0xD7, 0xFB,
    0x7C, 0xE3, 0x39, 0x82, 0x9B, 0x2F, 0xFF, 0x87, 0x34, 0x8E, 0x43, 0x44, 0xC4, 0xDE, 0xE9, 0xCB,
    0x54, 0x7B, 0x94, 0x32, 0xA6, 0xC2, 0x23, 0x3D, 0xEE, 0x4C, 0x95, 0x0B, 0x42, 0xFA, 0xC3, 0x4E,
    0x08, 0x2E, 0xA1, 0x66, 0x28, 0xD9, 0x24, 0xB2, 0x76, 0x5B, 0xA2, 0x49, 0x6D, 0x8B, 0xD1, 0x25,
    0x72, 0xF8, 0xF6, 0x64, 0x86, 0x68, 0x98, 0x16, 0xD4, 0xA4, 0x5C, 0xCC, 0x5D, 0x65, 0xB6, 0x92,
    0x6C, 0x70, 0x48, 0x50, 0xFD, 0xED, 0xB9, 0xDA, 0x5E, 0x15, 0x46, 0x57, 0xA7, 0x8D, 0x9D, 0x84,
    0x90, 0xD8, 0xAB, 0x00, 0x8C, 0xBC, 0xD3, 0x0A, 0xF7, 0xE4, 0x58, 0x05, 0xB8, 0xB3, 0x45, 0x06,
    0xD0, 0x2C, 0x1E, 0x8F, 0xCA, 0x3F, 0x0F, 0x02, 0xC1, 0xAF, 0xBD, 0x03, 0x01, 0x13, 0x8A, 0x6B,
    0x3A, 0x91, 0x11, 0x41, 0x4F, 0x67, 0xDC, 0xEA, 0x97, 0xF2, 0xCF, 0xCE, 0xF0, 0xB4, 0xE6, 0x73,
    0x96, 0xAC, 0x74, 0x22, 0xE7, 0xAD, 0x35, 0x85, 0xE2, 0xF9, 0x37, 0xE8, 0x1C, 0x75, 0xDF, 0x6E,
    0x47, 0xF1, 0x1A, 0x71, 0x1D, 0x29, 0xC5, 0x89, 0x6F, 0xB7, 0x62, 0x0E, 0xAA, 0x18, 0xBE, 0x1B,
    0xFC, 0x56, 0x3E, 0x4B, 0xC6, 0xD2, 0x79, 0x20, 0x9A, 0xDB, 0xC0, 0xFE, 0x78, 0xCD, 0x5A, 0xF4,
    0x1F, 0xDD, 0xA8, 0x33, 0x88, 0x07, 0xC7, 0x31, 0xB1, 0x12, 0x10, 0x59, 0x27, 0x80, 0xEC, 0x5F,
    0x60, 0x51, 0x7F, 0xA9, 0x19, 0xB5, 0x4A, 0x0D, 0x2D, 0xE5, 0x7A, 0x9F, 0x93, 0xC9, 0x9C, 0xEF,
    0xA0, 0xE0, 0x3B, 0x4D, 0xAE, 0x2A, 0xF5, 0xB0, 0xC8, 0xEB, 0xBB, 0x3C, 0x83, 0x53, 0x99, 0x61,
    0x17, 0x2B, 0x04, 0x7E, 0xBA, 0x77, 0xD6, 0x26, 0xE1, 0x69, 0x14, 0x63, 0x55, 0x21, 0x0C, 0x7D,
)
_RCON = (
    0x00, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40,
    0x80, 0x1B, 0x36, 0x6C, 0xD8, 0xAB, 0x4D, 0x9A,
)


def _xtime(a: int) -> int:
    return (((a << 1) ^ 0x1B) & 0xFF) if (a & 0x80) else (a << 1)


def _xor_bytes(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


def _bytes2matrix(text: bytes) -> list[list[int]]:
    return [list(text[i:i + 4]) for i in range(0, 16, 4)]


def _matrix2bytes(matrix: list[list[int]]) -> bytes:
    return bytes(sum(matrix, []))


def _sub_bytes(s: list[list[int]], box=_SBOX) -> None:
    for i in range(4):
        for j in range(4):
            s[i][j] = box[s[i][j]]


def _shift_rows(s: list[list[int]]) -> None:
    s[0][1], s[1][1], s[2][1], s[3][1] = s[1][1], s[2][1], s[3][1], s[0][1]
    s[0][2], s[1][2], s[2][2], s[3][2] = s[2][2], s[3][2], s[0][2], s[1][2]
    s[0][3], s[1][3], s[2][3], s[3][3] = s[3][3], s[0][3], s[1][3], s[2][3]


def _inv_shift_rows(s: list[list[int]]) -> None:
    s[0][1], s[1][1], s[2][1], s[3][1] = s[3][1], s[0][1], s[1][1], s[2][1]
    s[0][2], s[1][2], s[2][2], s[3][2] = s[2][2], s[3][2], s[0][2], s[1][2]
    s[0][3], s[1][3], s[2][3], s[3][3] = s[1][3], s[2][3], s[3][3], s[0][3]


def _mix_single_column(a: list[int]) -> None:
    t = a[0] ^ a[1] ^ a[2] ^ a[3]
    u = a[0]
    a[0] ^= t ^ _xtime(a[0] ^ a[1])
    a[1] ^= t ^ _xtime(a[1] ^ a[2])
    a[2] ^= t ^ _xtime(a[2] ^ a[3])
    a[3] ^= t ^ _xtime(a[3] ^ u)


def _mix_columns(s: list[list[int]]) -> None:
    for i in range(4):
        _mix_single_column(s[i])


def _inv_mix_columns(s: list[list[int]]) -> None:
    for i in range(4):
        u = _xtime(_xtime(s[i][0] ^ s[i][2]))
        v = _xtime(_xtime(s[i][1] ^ s[i][3]))
        s[i][0] ^= u
        s[i][1] ^= v
        s[i][2] ^= u
        s[i][3] ^= v
    _mix_columns(s)


def _add_round_key(s: list[list[int]], k: list[list[int]]) -> None:
    for i in range(4):
        for j in range(4):
            s[i][j] ^= k[i][j]


def _pad(plaintext: bytes) -> bytes:
    n = 16 - (len(plaintext) % 16)
    return plaintext + bytes([n] * n)


def _unpad(plaintext: bytes) -> bytes:
    n = plaintext[-1]
    if n < 1 or n > 16 or plaintext[-n:] != bytes([n] * n):
        raise ValueError("invalid PKCS#7 padding")
    return plaintext[:-n]


class _AES:
    """AES-128/192/256 with CBC + PKCS#7 (encrypt/decrypt only)."""

    _rounds = {16: 10, 24: 12, 32: 14}

    def __init__(self, master_key: bytes):
        self.n_rounds = self._rounds[len(master_key)]
        self._key_matrices = self._expand_key(master_key)

    def _expand_key(self, master_key: bytes) -> list[list[list[int]]]:
        key_columns = [list(master_key[i:i + 4]) for i in range(0, len(master_key), 4)]
        iteration_size = len(master_key) // 4
        i = 1
        while len(key_columns) < (self.n_rounds + 1) * 4:
            word = list(key_columns[-1])
            if len(key_columns) % iteration_size == 0:
                word.append(word.pop(0))
                word = [_SBOX[b] for b in word]
                word[0] ^= _RCON[i]
                i += 1
            elif len(master_key) == 32 and len(key_columns) % iteration_size == 4:
                word = [_SBOX[b] for b in word]
            word = list(_xor_bytes(bytes(word), bytes(key_columns[-iteration_size])))
            key_columns.append(word)
        return [key_columns[4 * r:4 * (r + 1)] for r in range(len(key_columns) // 4)]

    def encrypt_block(self, plaintext: bytes) -> bytes:
        state = _bytes2matrix(plaintext)
        _add_round_key(state, self._key_matrices[0])
        for r in range(1, self.n_rounds):
            _sub_bytes(state)
            _shift_rows(state)
            _mix_columns(state)
            _add_round_key(state, self._key_matrices[r])
        _sub_bytes(state)
        _shift_rows(state)
        _add_round_key(state, self._key_matrices[-1])
        return _matrix2bytes(state)

    def decrypt_block(self, ciphertext: bytes) -> bytes:
        state = _bytes2matrix(ciphertext)
        _add_round_key(state, self._key_matrices[-1])
        _inv_shift_rows(state)
        _sub_bytes(state, _INV_SBOX)
        for r in range(self.n_rounds - 1, 0, -1):
            _add_round_key(state, self._key_matrices[r])
            _inv_mix_columns(state)
            _inv_shift_rows(state)
            _sub_bytes(state, _INV_SBOX)
        _add_round_key(state, self._key_matrices[0])
        return _matrix2bytes(state)

    def encrypt_cbc_padded(self, plaintext: bytes, iv: bytes) -> bytes:
        data = _pad(plaintext)
        previous = iv
        out = []
        for i in range(0, len(data), 16):
            block = self.encrypt_block(_xor_bytes(data[i:i + 16], previous))
            out.append(block)
            previous = block
        return b"".join(out)

    def decrypt_cbc_padded(self, ciphertext: bytes, iv: bytes) -> bytes:
        previous = iv
        out = []
        for i in range(0, len(ciphertext), 16):
            block = ciphertext[i:i + 16]
            out.append(_xor_bytes(previous, self.decrypt_block(block)))
            previous = block
        return _unpad(b"".join(out))


_AES_INSTANCE: Optional[_AES] = None


def _aes() -> _AES:
    global _AES_INSTANCE
    if _AES_INSTANCE is None:
        _AES_INSTANCE = _AES(hashlib.sha256(WEBGUI_KEY).digest())
    return _AES_INSTANCE


def node_port_suffix(node_port, default: int = 15) -> int:
    try:
        return int(str(node_port).strip()) % 100
    except (TypeError, ValueError):
        return default


def derive_webgui_port(node_port) -> int:
    """Node 1015 -> DCS 10315 -> WebGUI 8815."""
    return WEBGUI_PORT_BASE + node_port_suffix(node_port)


def _encrypt_request(request: dict) -> bytes:
    iv = secrets.token_bytes(16)
    ct = _aes().encrypt_cbc_padded(json.dumps(request).encode("ascii"), iv)
    return json.dumps(
        {
            "iv": base64.b64encode(iv).decode("ascii"),
            "ct": base64.b64encode(ct).decode("ascii"),
        }
    ).encode("ascii")


def _decrypt_response(body: bytes) -> Any:
    data = json.loads(body)
    pt = _aes().decrypt_cbc_padded(
        base64.b64decode(data["ct"]),
        base64.b64decode(data["iv"]),
    )
    return json.loads(pt.decode("utf-8"))


def _is_admin_host_slot(pid, info: dict, server_id) -> bool:
    """Dedicated-server admin/host slot — always present, never a real client."""
    if server_id is not None:
        try:
            if int(pid) == int(server_id):
                return True
        except (TypeError, ValueError):
            if str(pid) == str(server_id):
                return True
    ipaddr = str(info.get("ipaddr") or "").strip()
    if ipaddr.startswith("0.0.0.0"):
        return True
    return False


def connected_dcs_clients(payload: Any) -> tuple[Optional[int], list[str]]:
    """Return (count, names) excluding the dedicated-server admin slot."""
    if not isinstance(payload, dict):
        return None, []
    players_root = payload.get("players")
    if not isinstance(players_root, dict):
        return None, []
    all_players = players_root.get("all")
    if all_players is None:
        return 0, []
    server_id = payload.get("server_id")
    names: list[str] = []
    if isinstance(all_players, dict):
        items = all_players.items()
    elif isinstance(all_players, list):
        items = ((p.get("id") if isinstance(p, dict) else idx, p) for idx, p in enumerate(all_players))
    else:
        return None, []
    for key, info in items:
        if not isinstance(info, dict):
            continue
        pid = info.get("id", key)
        if _is_admin_host_slot(pid, info, server_id):
            continue
        name = str(info.get("name") or "").strip()
        names.append(name or f"Player {pid}")
    return len(names), names


def webgui_request(request: dict, port: int, host: str = "127.0.0.1", timeout: float = 1.5) -> Any:
    body = _encrypt_request(request)
    conn = http.client.HTTPConnection(host, int(port), timeout=timeout)
    try:
        conn.request(
            "POST",
            "/encryptedRequest",
            body=body,
            headers={
                "Content-Type": "application/json",
                "Connection": "close",
                "Host": f"{host}:{int(port)}",
            },
        )
        resp = conn.getresponse()
        raw = resp.read()
        if resp.status != 200:
            raise RuntimeError(f"WebGUI HTTP {resp.status}")
        return _decrypt_response(raw)
    finally:
        conn.close()


def query_dcs_webgui_players(
    port: int,
    host: str = "127.0.0.1",
    timeout: float = 1.5,
) -> Optional[dict[str, Any]]:
    """Query local WebGUI getPlayers. Returns {count, names} or None on failure."""
    try:
        payload = webgui_request({"uri": "getPlayers"}, port=port, host=host, timeout=timeout)
        count, names = connected_dcs_clients(payload)
        if count is None:
            return None
        return {"count": count, "names": names}
    except Exception as e:
        logger.debug("WebGUI getPlayers failed on %s:%s: %s", host, port, e)
        return None

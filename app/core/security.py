import hashlib
import hmac


def generate_hmac_signature(payload_str: str, secret_key: str) -> str:
    """
    Sign a JSON payload string with the fest's secret_key using HMAC-SHA256.
    Returns the hex digest. The receiving fest backend should verify this
    against the X-Signature header.
    """
    return hmac.new(
        secret_key.encode("utf-8"),
        payload_str.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def generate_easebuzz_hash(
    key: str,
    txnid: str,
    amount: str,
    productinfo: str,
    firstname: str,
    email: str,
    salt: str,
    udf1: str = "",
    udf2: str = "",
    udf3: str = "",
    udf4: str = "",
    udf5: str = "",
) -> str:
    """
    EaseBuzz forward hash for payment initiation.

    Formula:
        sha512(key|txnid|amount|productinfo|firstname|email|
               udf1|udf2|udf3|udf4|udf5||||||salt)

    The 6 empty pipes after udf5 are mandatory (udf6–udf10 placeholders).
    `amount` must already be a string formatted to exactly 2 decimal places.
    """
    hash_str = (
        f"{key}|{txnid}|{amount}|{productinfo}|{firstname}|{email}|"
        f"{udf1}|{udf2}|{udf3}|{udf4}|{udf5}||||||{salt}"
    )
    return hashlib.sha512(hash_str.encode("utf-8")).hexdigest()


def verify_easebuzz_response_hash(data: dict, salt: str) -> bool:
    """
    Verify the reverse hash sent by EaseBuzz on the S2S webhook.

    Formula:
        sha512(salt|status||udf5|udf4|udf3|udf2|udf1|email|
               firstname|productinfo|amount|txnid|key)

    Note the double pipe after status (empty field placeholder).
    Returns True if computed hash matches the hash in the response.
    """
    received_hash = data.get("hash", "")
    hash_str = (
        f"{salt}|{data.get('status', '')}||"
        f"{data.get('udf5', '')}|{data.get('udf4', '')}|"
        f"{data.get('udf3', '')}|{data.get('udf2', '')}|"
        f"{data.get('udf1', '')}|{data.get('email', '')}|"
        f"{data.get('firstname', '')}|{data.get('productinfo', '')}|"
        f"{data.get('amount', '')}|{data.get('txnid', '')}|"
        f"{data.get('key', '')}"
    )
    computed = hashlib.sha512(hash_str.encode("utf-8")).hexdigest()
    return hmac.compare_digest(computed, received_hash)

#!/usr/bin/env python3
import base64
import struct

s = "BQHHXqgAtNqub65x0BVGYGY9amHiTwqEMyFz5W7R03YbMh0Jm3tCgu_lcVpIVHsdnRArmwr5NuSfAs_AZamSKCtVJFdkqhM6nD3ypT6D5u2nXnMwCkG20TF8TDViavr2Rte6IwbGrHIVJ8a3vO0JVSoWsu_8wrgOcKtzQwsPdUCoj5WUqFSD6pceg6j-022vczn9HsWewi4n5J5evT1LYkc8attWtIHRdlKNWTp8794HC1fIt7cMbyCE6QmLwKgkEn03iH7sVxdB6slPFb8c9eprio7-UGqYozuYbv8CytRW9gCYVxFmWfXUC5EzM0uSKj4cy9fiXR6B0avsqUkqyOcZnJwQAAAAINZOcNAA"

raw = base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
dc_id = raw[0]
api_id = struct.unpack('>I', raw[1:5])[0]
test_mode = bool(raw[5])
auth_key = raw[6:262]
remainder = raw[262:]

print(f"DC: {dc_id}")
print(f"API_ID: {api_id}")
print(f"TestMode: {test_mode}")
print(f"AuthKey length: {len(auth_key)}")
print(f"Remainder length: {len(remainder)}")
print(f"Remainder hex: {remainder.hex()}")

# If remainder is user_id:
# Is it 8 bytes (>q or >Q)?
user_id = struct.unpack('>q', remainder[:8])[0]
print(f"User ID as 8-byte int: {user_id}")

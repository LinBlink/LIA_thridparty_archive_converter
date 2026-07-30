import re

path = r"..\..\..\..\AppData\Local\Temp\tmp37e0fgse.json"

with open(path, "r", encoding="utf-8") as f:
    text = f.read()

# The content field spans multiple lines with literal newlines.
# Find the content value: from after '"content":' up to the closing quote
# that is followed by a comma and newline before the next key.
# Strategy: locate '"content":' then capture until the line that ends with '",
# ' followed by '"location_desc"'.

start_marker = '"content":'
start_idx = text.index(start_marker) + len(start_marker)

# Find the end: the quote+comma+newline+whitespace+"location_desc"
end_pattern = '",\n  "location_desc"'
end_idx = text.index(end_pattern)

# The raw content value (including the opening whitespace/newline and ending quote)
raw = text[start_idx:end_idx + 1]  # include the closing quote

# raw starts with whitespace/newline then the string content then ends with "
# Strip leading whitespace/newlines but preserve them as part of content? 
# The content begins right after '"content":' on line 4. There's a newline then content.
# We want to keep the text including newlines but escape them.

# Remove the leading newline/whitespace that separates key and value
# Actually the value starts with '\n  "我叫...' - the opening quote is after whitespace.
# Let's find the opening quote.
# After start_marker, skip whitespace and newlines until we hit the first '"'
i = start_idx
while i < len(text) and text[i] != '"':
    i += 1
opening_quote_idx = i

# The content string is from opening_quote_idx+1 to end_idx (end_idx points at the closing '"')
content_str = text[opening_quote_idx + 1:end_idx]

# Escape newlines and backslashes
escaped = content_str.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r")

# Rebuild
new_text = text[:opening_quote_idx + 1] + escaped + text[end_idx:]

with open(path, "w", encoding="utf-8") as f:
    f.write(new_text)

# Verify
import json
with open(path, "r", encoding="utf-8") as f:
    json.load(f)
print("JSON is valid after fix.")
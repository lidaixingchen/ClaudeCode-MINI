import pathlib

p = pathlib.Path("e:/project/ClaudeCode-MINI/example/docs/03-tools.md")
content = p.read_text(encoding="utf-8")

# Line 143 has: 弯引号（`"）
# pos 64: backtick, pos 65: straight ", pos 66: backtick
# We need pos 65 to be U+201C (left double quotation mark)
# Use the surrounding context to make a unique match

old = chr(0x5F2F) + chr(0x5F15) + chr(0x53F7) + chr(0xFF08) + chr(0x0060) + chr(0x0022) + chr(0x0060) + chr(0xFF09)
#       弯          引          号          （          `          "          `          ）

new = old[:5] + chr(0x201C) + old[6:]
#                       `          "          `

if old in content:
    content = content.replace(old, new)
    p.write_text(content, encoding="utf-8")
    print("Fixed.")
else:
    print("Pattern not found.")

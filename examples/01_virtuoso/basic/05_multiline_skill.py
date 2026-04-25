"""
05_multiline_skill.py — Test multi-line SKILL execution with comments.

Verifies that execute_skill() correctly handles:
- Multi-line expressions
- Full-line comments (;)
- Inline comments (x = 10 ; comment)
- Semicolons inside strings (not comments)
- Procedure definitions spanning multiple lines
- Nested let/for/progn blocks

Each test prints to Virtuoso CIW so you can see execution on the remote side.
"""

from virtuoso_bridge import VirtuosoClient

client = VirtuosoClient.from_env()

passed = 0
total = 0


def check(name, got, expected):
    global passed, total
    total += 1
    got = str(got).strip()
    expected = str(expected).strip()
    ok = got == expected
    passed += ok
    icon = "PASS" if ok else "FAIL"
    print(f"  [{icon}] {name}")
    if not ok:
        print(f"         got:      {got}")
        print(f"         expected: {expected}")


print(f"\n{'='*60}")
print(f"Multi-line SKILL Tests")
print(f"{'='*60}\n")


# --- Test 1: simple multiline arithmetic ---
skill_cmd = """
let((result)
    result = (1
        +2
        +3)
    printf("\\n[Test 1] multiline arithmetic: %d\\n" result)
    result
)
"""
r = client.execute_skill(skill_cmd)
check("multiline arithmetic", r.output, "6")


# --- Test 2: sprintf with escaped newlines ---
skill_cmd = """
let((s)
    s = sprintf(nil "line1: %d\\nline2: %d\\nline3: %d" 10 20 30)
    printf("\\n[Test 2] sprintf multiline:\\n%s\\n" s)
    s
)
"""
r = client.execute_skill(skill_cmd)
check("sprintf multiline string", "line1" in r.output, "True")


# --- Test 3: let block with full-line comments ---
skill_cmd = """
let((a b c)
    ; this is a comment
    a = 10
    b = 20
    ; another comment
    c = a + b
    printf("\\n[Test 3] let + comments: %d + %d = %d\\n" a b c)
    c
)
"""
r = client.execute_skill(skill_cmd)
check("let + full-line comments", r.output, "30")


# --- Test 4: for loop with comments ---
skill_cmd = """
let((result)
    ; compute sum 1..10
    result = 0
    for(i 1 10
        result = result + i
    )
    ; print and return result
    printf("\\n[Test 4] sum(1..10) = %d\\n" result)
    result
)
"""
r = client.execute_skill(skill_cmd)
check("for loop + comments", r.output, "55")


# --- Test 5: list operations ---
skill_cmd = """
let((mylist filtered s)
    ; create a list
    mylist = '(1 2 3 4 5 6 7 8 9 10)
    ; filter even numbers
    filtered = setof(x mylist (evenp x))
    s = sprintf(nil "%L" filtered)
    printf("\\n[Test 5] even numbers from 1..10: %s\\n" s)
    s
)
"""
r = client.execute_skill(skill_cmd)
check("list filter", "(2 4 6 8 10)" in r.output, "True")


# --- Test 6: string containing semicolons (must NOT be treated as comments) ---
skill_cmd = """
let((s n)
    s = "hello; world; test"
    n = strlen(s)
    printf("\\n[Test 6] string with semicolons: \\"%s\\" len=%d\\n" s n)
    n
)
"""
r = client.execute_skill(skill_cmd)
check("string with semicolons", r.output, "18")


# --- Test 7: inline comments ---
skill_cmd = """
let((a b c d s)
    a = 100       ; first value
    b = a * 2     ; double it
    c = b - 50    ; subtract
    d = c / 10    ; divide
    s = sprintf(nil "a=%d b=%d c=%d d=%d" a b c d)
    printf("\\n[Test 7] inline comments: %s\\n" s)
    s
)
"""
r = client.execute_skill(skill_cmd)
check("inline comments", "a=100 b=200 c=150 d=15" in r.output, "True")


# --- Test 8: procedure definition and call ---
skill_cmd = """
procedure(_vb_test_add(x y)
    ; add two numbers
    let((result)
        result = x + y
        printf("\\n[Test 8] _vb_test_add(%d, %d) = %d\\n" x y result)
        result
    )
)
_vb_test_add(17 25)
"""
r = client.execute_skill(skill_cmd)
check("procedure def + call", r.output, "42")


# --- Summary ---
print(f"\n{passed}/{total} passed\n")

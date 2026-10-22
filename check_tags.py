from html.parser import HTMLParser

class TagChecker(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.void_tags = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}
        self.errors = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() not in self.void_tags:
            line, col = self.getpos()
            self.stack.append((tag.lower(), line, dict(attrs).get('id', '')))

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self.void_tags:
            return
        if not self.stack:
            line, col = self.getpos()
            self.errors.append(f'EXTRA CLOSING TAG: </{tag}> at line {line}')
            return
        last_tag, line, tid = self.stack.pop()
        if last_tag != tag:
            cur_line, col = self.getpos()
            self.errors.append(f'MISMATCH at line {cur_line}: expected </{last_tag}> (opened line {line} id="{tid}"), got </{tag}>')

with open('app/webapp/static/index.html', encoding='utf-8') as f:
    content = f.read()

checker = TagChecker()
checker.feed(content)
print(f"Tag errors count: {len(checker.errors)}")
for e in checker.errors:
    print(e)
if checker.stack:
    print(f"UNCLOSED TAGS at EOF: {len(checker.stack)}")
    for t, l, tid in checker.stack[-15:]:
        print(f'  <{t}> opened at line {l} id="{tid}"')
else:
    print("SUCCESS: ALL HTML TAGS ARE PERFECTLY BALANCED AND NESTED!")

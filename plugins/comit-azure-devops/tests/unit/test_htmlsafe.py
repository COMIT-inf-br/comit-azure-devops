"""HTML seguro do comentário e comparação na releitura."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "server"))
from adoplug import htmlsafe  # noqa: E402

ORG = "OrgA"
PROJ = "0c0c0c0c-0000-4000-8000-00000000000c"
PROJ2 = "0d0d0d0d-0000-4000-8000-00000000000d"
ID = "1a1a1a1a-0000-4000-8000-00000000001a"


class MontagemTest(unittest.TestCase):
    def test_texto_escapado_com_quebra_e_link(self):
        h = htmlsafe.text_to_html('a <b> & "c"\nveja https://example.com/x?a=1&b=2.')
        self.assertEqual(h, 'a &lt;b&gt; &amp; &quot;c&quot;<br>veja '
                            '<a href="https://example.com/x?a=1&amp;b=2" target="_blank">https://example.com/x?a=1&amp;b=2</a>.')

    def test_parentese_da_url_e_da_frase(self):
        h = htmlsafe.text_to_html("ver https://e.com/a(b) e (https://e.com/c).")
        self.assertIn('<a href="https://e.com/a(b)" target="_blank">https://e.com/a(b)</a> e (', h)
        self.assertTrue(h.endswith('<a href="https://e.com/c" target="_blank">https://e.com/c</a>).'), h)

    def test_mencao(self):
        self.assertEqual(htmlsafe.mention(ID.upper(), "Ana <x>"),
                         f'<a href="#" data-vss-mention="version:2.0,{ID}">@Ana &lt;x&gt;</a>')
        with self.assertRaises(htmlsafe.HtmlError):
            htmlsafe.mention("nao-guid", "Ana")

    def test_link_so_http(self):
        with self.assertRaises(htmlsafe.HtmlError):
            htmlsafe.link("javascript:alert(1)")
        with self.assertRaises(htmlsafe.HtmlError):
            htmlsafe.link("https://user:pw@example.com/")


class SanitizeTest(unittest.TestCase):
    def test_tags_permitidas_e_target_blank(self):
        h = htmlsafe.sanitize('<p>Oi <strong>x</strong><br><a href="https://e.com/a(b)">l</a></p><ul><li>1</li></ul>')
        self.assertEqual(h, '<p>Oi <strong>x</strong><br><a href="https://e.com/a(b)" target="_blank">l</a></p>'
                            '<ul><li>1</li></ul>')

    def test_entidade_reescapada(self):
        self.assertEqual(htmlsafe.sanitize("&lt;script&gt;"), "&lt;script&gt;")

    def test_mencao_aceita(self):
        m = f'<a href="#" data-vss-mention="version:2.0,{ID}">@Ana</a>'
        self.assertEqual(htmlsafe.sanitize(m), m)

    def test_recusas(self):
        for bad in ("<script>x</script>", '<p onclick="x">a</p>', '<p style="x">a</p>', '<a href="javascript:x">a</a>',
                    '<img src="https://e.com/a.png">', "<!-- c -->", "<p>aberto", "</p>", '<a href="#" data-vss-mention="x">a</a>',
                    '<a href="ftp://e.com">a</a>', '<a href="https://e.com" href="https://f.com">a</a>'):
            with self.subTest(bad=bad), self.assertRaises(htmlsafe.HtmlError) as cm:
                htmlsafe.sanitize(bad)
            self.assertEqual(cm.exception.reason_code, "HTML_NOT_ALLOWED")

    def test_to_text(self):
        t = htmlsafe.to_text('<div>Oi</div><ul><li>a</li></ul>x<br>y <a href="https://e.com">l</a>')
        self.assertEqual(t, "Oi\n\n- a\nx\ny l (https://e.com)")


class EquivalenteTest(unittest.TestCase):
    def test_target_sem_aspas_e_quot(self):
        sent = '<a href="https://e.com" target="_blank">"x"</a>'
        got = '<a href="https://e.com" target=_blank>&quot;x&quot;</a>'
        self.assertTrue(htmlsafe.equivalent(got, sent))

    def test_parenteses_so_no_href(self):
        self.assertTrue(htmlsafe.equivalent('<a href="https://e.com/a%28b%29">x</a>', '<a href="https://e.com/a(b)">x</a>'))
        self.assertFalse(htmlsafe.equivalent("<p>%28</p>", "<p>(</p>"))
        self.assertFalse(htmlsafe.equivalent('<a href="https://e.com/a%2Fb">x</a>', '<a href="https://e.com/a/b">x</a>'))

    def test_anexo_com_projeto(self):
        a = "https://dev.azure.com/OrgA/_apis/wit/attachments/" + PROJ2
        got_p = "https://dev.azure.com/OrgA/" + PROJ + "/_apis/wit/attachments/" + PROJ2
        sent = f'<a href="{a}">a</a> {a}'
        got = f'<a href="{got_p}">a</a> {got_p}'
        self.assertTrue(htmlsafe.equivalent(got, sent, ORG, PROJ))
        self.assertFalse(htmlsafe.equivalent(got, sent))  # sem projeto, sem tolerância
        other = got.replace(PROJ, PROJ2)
        self.assertFalse(htmlsafe.equivalent(other, sent, ORG, PROJ))

    def test_espaco_no_fim_de_bloco(self):
        self.assertTrue(htmlsafe.equivalent("<p>a</p>", "<p>a </p>"))
        self.assertFalse(htmlsafe.equivalent("<pre>a</pre>", "<pre>a </pre>"))

    def test_atributos_nao_medidos_diferem(self):
        self.assertFalse(htmlsafe.equivalent('<img src="b">', '<img src="a">'))
        self.assertFalse(htmlsafe.equivalent('<p class="x">a</p>', '<p class="y">a</p>'))
        self.assertFalse(htmlsafe.equivalent("<p>a</p>", "<p>b</p>"))


if __name__ == "__main__":
    unittest.main()

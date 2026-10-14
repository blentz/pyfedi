import re
import time
import unittest

import markdown2
from bs4 import BeautifulSoup
from app import markdown_extras
from app.markdown_extras import EnhancedImages, apply_enhanced_image_attributes
from tests.test_markdown_to_html import corpus_texts, random_texts


def markdown_with_enhanced_images(text, extras_list=None):
    """Helper function to convert markdown with enhanced images support"""
    if extras_list is None:
        extras_list = ['enhanced-images']
    extras_dict = {extra: True for extra in extras_list}
    md = markdown2.Markdown(extras=extras_dict)
    html = md.convert(text)
    # Apply enhanced image attributes
    html = apply_enhanced_image_attributes(html, md)
    return html


class TestEnhancedImages(unittest.TestCase):

    def test_image_with_align_left_and_width(self):
        """Test image with align-left and width attribute"""
        markdown = "![A cute cat :: align-left width=200px](cat.jpg)"
        result = markdown_with_enhanced_images(markdown)
        self.assertIn('align="left"', result)
        self.assertIn('width="200px"', result)
        self.assertIn('alt="A cute cat"', result)
        self.assertIn('src="cat.jpg"', result)

    def test_image_with_align_right_and_percentage_width(self):
        """Test image with align-right and percentage width"""
        markdown = "![Logo :: align-right width=50%](logo.png)"
        result = markdown_with_enhanced_images(markdown)
        self.assertIn('align="right"', result)
        self.assertIn('width="50%"', result)
        self.assertIn('alt="Logo"', result)
        self.assertIn('src="logo.png"', result)

    def test_image_with_align_center_width_and_height(self):
        """Test image with align-center, width, and height"""
        markdown = "![Banner :: align-center width=800px height=200px](banner.jpg)"
        result = markdown_with_enhanced_images(markdown)
        self.assertIn('align="center"', result)
        self.assertIn('width="800px"', result)
        self.assertIn('height="200px"', result)
        self.assertIn('alt="Banner"', result)
        self.assertIn('src="banner.jpg"', result)

    def test_regular_image_without_custom_attributes(self):
        """Test that regular markdown images still work"""
        markdown = "![Regular image](regular.jpg)"
        result = markdown_with_enhanced_images(markdown)
        self.assertIn('alt="Regular image"', result)
        self.assertIn('src="regular.jpg"', result)
        # Should not have any custom attributes
        self.assertNotIn('align=', result)
        self.assertNotIn('width=', result)
        self.assertNotIn('height=', result)

    def test_image_with_class_attribute(self):
        """Test image with CSS class attribute"""
        markdown = "![Styled :: align-left class=thumbnail](thumb.jpg)"
        result = markdown_with_enhanced_images(markdown)
        self.assertIn('align="left"', result)
        self.assertIn('class="thumbnail"', result)
        self.assertIn('alt="Styled"', result)
        self.assertIn('src="thumb.jpg"', result)

    def test_image_with_all_attributes(self):
        """Test image with all supported attributes"""
        markdown = "![Full example :: align-center width=300px height=200px class=fancy](example.jpg)"
        result = markdown_with_enhanced_images(markdown)
        self.assertIn('align="center"', result)
        self.assertIn('width="300px"', result)
        self.assertIn('height="200px"', result)
        self.assertIn('class="fancy"', result)
        self.assertIn('alt="Full example"', result)
        self.assertIn('src="example.jpg"', result)

    def test_image_without_enhanced_extras(self):
        """Test that :: delimiter in alt text works normally without the extras"""
        markdown = "![Alt text :: some data](image.jpg)"
        result = markdown2.markdown(markdown)
        # Without the extras, the :: should be preserved in alt text
        self.assertIn('alt="Alt text :: some data"', result)
        self.assertNotIn('align=', result)

    def test_image_with_title(self):
        """Test enhanced image with title attribute"""
        markdown = '![Cat :: align-left width=200px](cat.jpg "A cute cat")'
        result = markdown_with_enhanced_images(markdown)
        self.assertIn('align="left"', result)
        self.assertIn('width="200px"', result)
        self.assertIn('alt="Cat"', result)
        self.assertIn('title="A cute cat"', result)
        self.assertIn('src="cat.jpg"', result)

    def test_multiple_images_in_same_document(self):
        """Test multiple enhanced images in the same markdown"""
        markdown = """
![First :: align-left width=100px](first.jpg)

Some text here.

![Second :: align-right width=200px](second.jpg)
"""
        result = markdown_with_enhanced_images(markdown)
        # First image
        self.assertIn('alt="First"', result)
        self.assertIn('align="left"', result)
        self.assertIn('width="100px"', result)
        self.assertIn('src="first.jpg"', result)
        # Second image
        self.assertIn('alt="Second"', result)
        self.assertIn('align="right"', result)
        self.assertIn('width="200px"', result)
        self.assertIn('src="second.jpg"', result)

    def test_image_with_width_no_units(self):
        """Test image with width value without units"""
        markdown = "![Image :: width=500](image.jpg)"
        result = markdown_with_enhanced_images(markdown)
        self.assertIn('width="500"', result)
        self.assertIn('alt="Image"', result)

    def test_image_with_em_units(self):
        """Test image with em units for width"""
        markdown = "![Image :: width=20em height=15em](image.jpg)"
        result = markdown_with_enhanced_images(markdown)
        self.assertIn('width="20em"', result)
        self.assertIn('height="15em"', result)

    def test_thumbnail_with_fullsize(self):
        """Test thumbnail image linking to fullsize"""
        markdown = "![Cat :: width=200px](thumb.jpg, full.jpg)"
        result = markdown_with_enhanced_images(markdown)
        self.assertIn('<a href="full.jpg">', result)
        self.assertIn('src="thumb.jpg"', result)
        self.assertIn('width="200px"', result)
        self.assertIn('alt="Cat"', result)
        # Img should be inside anchor tag
        self.assertIn('</a>', result)

    def test_thumbnail_with_fullsize_and_attributes(self):
        """Test thumbnail with fullsize and multiple attributes"""
        markdown = "![Cat :: align-left width=200px class=thumbnail](thumb.jpg, full.jpg)"
        result = markdown_with_enhanced_images(markdown)
        self.assertIn('<a href="full.jpg">', result)
        self.assertIn('src="thumb.jpg"', result)
        self.assertIn('align="left"', result)
        self.assertIn('width="200px"', result)
        self.assertIn('class="thumbnail"', result)
        self.assertIn('alt="Cat"', result)

    def test_thumbnail_with_fullsize_and_title(self):
        """Test thumbnail with fullsize and title attribute"""
        markdown = '![Cat :: width=200px](thumb.jpg, full.jpg "A cute cat")'
        result = markdown_with_enhanced_images(markdown)
        self.assertIn('<a href="full.jpg">', result)
        self.assertIn('src="thumb.jpg"', result)
        self.assertIn('width="200px"', result)
        self.assertIn('title="A cute cat"', result)
        self.assertIn('alt="Cat"', result)

    def test_single_image_no_anchor_tag(self):
        """Test that single images don't get wrapped in anchor tag"""
        markdown = "![Cat :: width=200px](cat.jpg)"
        result = markdown_with_enhanced_images(markdown)
        self.assertIn('<img', result)
        self.assertIn('src="cat.jpg"', result)
        # Should NOT have anchor tag
        self.assertNotIn('<a href=', result)

    def test_blank_url_gives_an_empty_src(self):
        """A url of only spaces, or only a full-size url after the comma, was url_and_title.split()[0] on an empty
        list: IndexError out of markdown_to_html for a post or a peer's markdown (fuzz: enhanced_images/blank_url,
        enhanced_images/only_full_url, markdown_to_html/enhanced_image_blank_url). It now gives src="", as a url of
        only a quoted title already did."""
        self.assertIn('src=""', markdown_with_enhanced_images('![a :: b](   )'))
        result = markdown_with_enhanced_images('![a :: b](, full.jpg)')
        self.assertIn('src=""', result)
        self.assertIn('href="full.jpg"', result)

    def test_finds_the_images_the_old_pattern_found(self):
        """The old pattern backtracked over every '::' and from every '![' (fuzz: enhanced_images/separator_pump,
        open_image_pump, unclosed_link_pump)."""
        old = re.compile(r'!\[([^\]]*?)\s*::\s*([^\]]+?)\]\(([^\)]+?)\)', re.MULTILINE)
        new = EnhancedImages._md_img_with_attrs_re
        fragments = ['![a :: b](c)', '![', '::', ':', ']', '(', ')', '](', 'a', ' ', '\n', '"t"', ',']
        for text in random_texts(fragments, 6000, 12, seed=12) + corpus_texts('enhanced_images', 300):
            expected = [(m.span(), m.groups()) for m in old.finditer(text)]
            self.assertEqual([(m.span(), m.groups()) for m in new.finditer(text) if m.lastindex is not None],
                             expected, repr(text))

    def test_unclosed_images_are_read_in_linear_time(self):
        """The extra alone: markdown2's own link and image regexes are quadratic on the last two shapes too, which
        is markdown2's to fix, not this module's."""
        size = 64 * 1024
        for text in ('![' + '::' * (size // 2), '![' * (size // 2) + '::', '![a::b](' * (size // 8)):
            md = markdown2.Markdown(extras={'enhanced-images': True})
            md.reset()
            started = time.perf_counter()
            EnhancedImages(md, None).run(text)
            self.assertLess(time.perf_counter() - started, 0.5)

    def test_finds_the_tags_the_old_pattern_found(self):
        """apply_enhanced_image_attributes' tag pattern backtracked over the spaces after '<img' and over every
        data-enhanced-img=, from every '<img' (fuzz: enhanced_images/img_tag_pump)."""
        old = re.compile(r'<img\s+([^>]*?data-enhanced-img=[^>]*?)(/?>)', re.IGNORECASE)
        new = markdown_extras._ENHANCED_IMG_TAG_RE
        fragments = ['<img ', '<IMG\t', 'data-enhanced-img="e-1"', 'DATA-ENHANCED-IMG=', ' ', '/', '>', '/>', 'a', '\n']
        for text in random_texts(fragments, 6000, 10, seed=13) + corpus_texts('enhanced_images', 300):
            expected = [(m.span(), m.groups()) for m in old.finditer(text)]
            self.assertEqual([(m.span(), m.groups()) for m in new.finditer(text) if m.lastindex is not None],
                             expected, repr(text))

    def test_unclosed_img_tags_are_read_in_linear_time(self):
        md = markdown2.Markdown(extras={'enhanced-images': True})
        md.convert('![a :: width=1](u)')
        size = 64 * 1024
        for html in ('<img ' * (size // 5), '<img' + ' ' * size, '<img data-enhanced-img=' * (size // 23)):
            started = time.perf_counter()
            apply_enhanced_image_attributes(html, md)
            self.assertLess(time.perf_counter() - started, 0.5)

    def test_a_quote_in_a_value_cannot_add_an_attribute(self):
        """Every value from the markdown was put between double quotes as it was, so a '"' in the alt text, a
        width, a height or the full-size url closed the attribute and the rest became attributes of the author's
        choosing: ![x" onerror="alert(1) :: w](u) built <img ... alt="x" onerror="alert(1)" ...> (fuzz:
        enhanced_images/quote_in_alt, quote_in_width). allowlist_html strips the handler later, but the extra's
        own output must not carry one."""
        img = BeautifulSoup(markdown_with_enhanced_images('![x" onerror="alert(1) :: width=1"onload=y](u, f"z)'),
                            'html.parser').find('img')
        self.assertEqual(img['alt'], 'x" onerror="alert(1)')
        self.assertEqual(img['width'], '1"onload=y')
        self.assertEqual(sorted(img.attrs), ['alt', 'src', 'width'])
        self.assertEqual(img.parent['href'], 'f"z')

    def test_an_angle_bracket_in_a_value_keeps_the_attributes(self):
        """apply_enhanced_image_attributes finds the built <img> by reading up to its first '>', so a '>' in the url
        or the alt text hid the marker: the image lost its width, alignment and class and kept
        data-enhanced-img (fuzz: enhanced_images/angle_bracket_in_url)."""
        img = BeautifulSoup(markdown_with_enhanced_images('![a>b :: align-left width=200px](x>y.jpg)'),
                            'html.parser').find('img')
        self.assertEqual((img['src'], img['alt'], img['width'], img['align']), ('x>y.jpg', 'a>b', '200px', 'left'))
        self.assertNotIn('data-enhanced-img', img.attrs)

    def test_integration_with_markdown_to_html(self):
        """Test integration with markdown_to_html function (full extras suite)"""
        from app.utils import markdown_to_html
        markdown = 'Testing!\n\n![an image :: width=50](https://piefed.social/static/media/logo_8p7en.svg)\n\nthere we go'
        result = markdown_to_html(markdown)
        print(f"Result: {result}")
        # Should have proper width attribute
        self.assertIn('width="50"', result)
        # Should have proper src (without extra quotes)
        self.assertIn('src=', result)
        self.assertIn('https://piefed.social/static/media/logo_8p7en.svg', result)
        # Should have proper alt text
        self.assertIn('alt=', result)
        self.assertIn('an image', result)
        # Should NOT have data-enhanced-img in final output
        self.assertNotIn('data-enhanced-img', result)
        # Should NOT have broken quotes like src="'url'"
        self.assertNotIn("src=\"'", result)
        self.assertNotIn("alt=\"'", result)


if __name__ == '__main__':
    unittest.main()

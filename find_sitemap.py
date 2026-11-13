import urllib.request
import re
import xml.etree.ElementTree as ET

sitemap_url = "https://www.samsung.com/in/sitemap.xml"
headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
}

req = urllib.request.Request(sitemap_url, headers=headers)
try:
    with urllib.request.urlopen(req) as resp:
        xml_content = resp.read()
        print("Sitemap length:", len(xml_content))
        # Find sub-sitemaps
        sub_sitemaps = re.findall(r'<loc>(https://[^\s<>]+\.xml)</loc>', xml_content.decode('utf-8', errors='ignore'))
        print("Sub sitemaps:", len(sub_sitemaps))
        for sm in sub_sitemaps:
            print(" ", sm)
            if 'product' in sm.lower() or 'computer' in sm.lower():
                # fetch it
                s_req = urllib.request.Request(sm, headers=headers)
                with urllib.request.urlopen(s_req) as s_resp:
                    s_xml = s_resp.read().decode('utf-8', errors='ignore')
                    laptop_urls = re.findall(r'<loc>(https://www\.samsung\.com/in/computers/[^\s<>]+)</loc>', s_xml)
                    print(f"  Found {len(laptop_urls)} computer URLs in {sm}")
                    galaxy_urls = [u for u in laptop_urls if 'galaxy-book' in u]
                    print(f"  Found {len(galaxy_urls)} Galaxy Book URLs!")
                    for gu in galaxy_urls[:10]:
                        print("    ", gu)
except Exception as e:
    print("Error:", e)


# mock_server.py
import http.server, socketserver, os, textwrap, pathlib

ROOT = pathlib.Path(__file__).parent / "mock_onlyfive"

def write(path: pathlib.Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")

def ensure_mock_files():
    # クリエイター一覧（上位5件の posts へのリンク）
    write(ROOT / "creators/3544/index.html", """
    <!doctype html><meta charset="utf-8">
    <title>Mock ONLY FIVE - Creator 3544</title>
    <h1>Mock ONLY FIVE - Creator 3544</h1>
    <a href="/posts/1001"><div class="post">#1 old sold out</div></a>
    <a href="/posts/1002"><div class="post">#2 old sold out</div></a>
    <a href="/posts/1003"><div class="post">#3 NEW (BUYABLE)</div></a>
    <a href="/posts/1004"><div class="post">#4 old sold out</div></a>
    <a href="/posts/1005"><div class="post">#5 old sold out</div></a>
    """)

    # 売り切れページ（1001,1002,1004,1005）
    for pid in [1001, 1002, 1004, 1005]:
        write(ROOT / f"posts/{pid}/index.html", f"""
        <!doctype html><meta charset="utf-8">
        <title>Post {pid}</title>
        <h2>Post {pid}</h2>
        <a class="buy-button disabled" href="#">売り切れてます</a>
        """)

    # 購入可ページ（1003）
    write(ROOT / "posts/1003/index.html", """
    <!doctype html><meta charset="utf-8">
    <title>Post 1003</title>
    <h2>Post 1003</h2>
    <a class="buy-button" href="/posts/1003/charge_confirmation">購入する</a>
    """)

    # 確認ページ
    write(ROOT / "posts/1003/charge_confirmation/index.html", """
    <!doctype html><meta charset="utf-8">
    <title>Charge Confirmation (Mock)</title>
    <h2>Charge Confirmation (Mock)</h2>
    <p>ここで決済確定はしません。検証はここまで。</p>
    """)

def main():
    ensure_mock_files()
    os.chdir(ROOT)  # static 配信ルートを mock_onlyfive に
    port = 8000
    handler = http.server.SimpleHTTPRequestHandler
    with socketserver.TCPServer(("", port), handler) as httpd:
        print(f"[mock] http://localhost:{port}/creators/3544/ を開いてください")
        httpd.serve_forever()

if __name__ == "__main__":
    main()

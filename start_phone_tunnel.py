import subprocess
import re
import sys
import os
import webbrowser
import time
import shutil

def main():
    print("=" * 60)
    print("   MEDIGUARD AI - KHOI DONG TRUY CAP TU DIEN THOAI (5G/4G)")
    print("=" * 60)
    print("\n[1/3] Dang kiem tra cloudflared.exe...")
    
    cloudflared_bin = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cloudflared.exe")
    if not os.path.exists(cloudflared_bin):
        system_bin = shutil.which("cloudflared")
        if system_bin:
            cloudflared_bin = system_bin
        else:
            print("[*] Dang tu dong tai cloudflared.exe tu Cloudflare GitHub Release...")
            try:
                import urllib.request
                dl_url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
                urllib.request.urlretrieve(dl_url, cloudflared_bin)
                print("[V] Da tai thanh cong cloudflared.exe!")
            except Exception as e:
                print(f"[!] Khong the tu dong tai cloudflared.exe: {e}")
                print("    Vui long tai thu cong tu https://github.com/cloudflare/cloudflared/releases va copy vao thu muc du an.")
                sys.exit(1)
        
    print("[2/3] Dang tao duong ham Cloudflare Tunnel toi http://localhost:5173...")
    cmd = [cloudflared_bin, "tunnel", "--url", "http://localhost:5173"]
    
    # Cloudflare in thong tin tunnel ra stderr
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        encoding="utf-8",
        errors="replace"
    )
    
    tunnel_url = None
    url_pattern = re.compile(r'https://[a-zA-Z0-9-]+\.trycloudflare\.com')
    
    for line in iter(process.stdout.readline, ''):
        match = url_pattern.search(line)
        if match:
            tunnel_url = match.group(0)
            break
        if "ERR" in line or "error" in line.lower():
            print(f"[Log] {line.strip()}")
            
    if tunnel_url:
        print("\n" + "=" * 60)
        print("  THANH CONG! DUONG LINK TRUY CAP 5G CHO DIEN THOAI:")
        print(f"  👉 {tunnel_url}")
        print("=" * 60)
        
        # 1. Copy link vao Clipboard cua Windows
        try:
            subprocess.run(["clip"], input=tunnel_url.strip(), text=True, check=False)
            print("\n[V] Da tu dong copy link vao bo nho tam (Clipboard).")
            print("    Ban co the mo Zalo / Messenger tren may tinh va bam Ctrl + V de gui cho chinh minh!")
        except Exception:
            pass
            
        # 2. Tao file HTML hien thi QR Code va tu dong mo trinh duyet
        qr_html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "phone_access.html")
        qr_api_url = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data={tunnel_url}"
        
        html_content = f"""<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Quét mã mở MediGuard AI trên Điện thoại (5G)</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
            background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%);
            color: #ffffff;
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 100vh;
            margin: 0;
            padding: 20px;
            box-sizing: border-box;
        }}
        .card {{
            background: rgba(30, 41, 59, 0.9);
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 24px;
            padding: 36px;
            max-width: 480px;
            width: 100%;
            text-align: center;
            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);
            backdrop-filter: blur(16px);
        }}
        .badge {{
            display: inline-block;
            background: #10b981;
            color: #ffffff;
            padding: 6px 14px;
            border-radius: 999px;
            font-size: 13px;
            font-weight: 700;
            letter-spacing: 0.5px;
            margin-bottom: 16px;
        }}
        h1 {{
            font-size: 24px;
            margin: 0 0 10px 0;
            font-weight: 800;
            color: #38bdf8;
        }}
        p.subtitle {{
            color: #94a3b8;
            font-size: 14px;
            margin-bottom: 24px;
            line-height: 1.5;
        }}
        .qr-wrapper {{
            background: #ffffff;
            padding: 16px;
            border-radius: 16px;
            display: inline-block;
            margin-bottom: 20px;
            box-shadow: 0 10px 25px rgba(0, 0, 0, 0.3);
        }}
        .qr-wrapper img {{
            display: block;
            width: 240px;
            height: 240px;
        }}
        .url-box {{
            background: #0f172a;
            border: 1px solid #334155;
            border-radius: 12px;
            padding: 12px 16px;
            font-family: monospace;
            font-size: 14px;
            color: #38bdf8;
            word-break: break-all;
            margin-bottom: 20px;
            user-select: all;
        }}
        .instructions {{
            text-align: left;
            background: rgba(15, 23, 42, 0.6);
            border-radius: 12px;
            padding: 16px 20px;
            font-size: 13px;
            color: #cbd5e1;
            line-height: 1.6;
        }}
        .instructions ol {{
            margin: 8px 0 0 0;
            padding-left: 20px;
        }}
        .instructions li {{
            margin-bottom: 6px;
        }}
        .highlight {{
            color: #fbbf24;
            font-weight: 600;
        }}
    </style>
</head>
<body>
    <div class="card">
        <div class="badge">🌐 KẾT NỐI 5G / INTERNET SẴN SÀNG</div>
        <h1>MediGuard AI Mobile</h1>
        <p class="subtitle">Bật <b>5G / 4G</b> trên điện thoại rồi mở ứng dụng bằng cách quét mã hoặc bấm vào link dưới đây:</p>
        
        <div class="qr-wrapper">
            <img src="{qr_api_url}" alt="QR Code MediGuard AI" />
        </div>
        
        <div class="url-box">
            <a href="{tunnel_url}" target="_blank" style="color: #38bdf8; text-decoration: none;">{tunnel_url}</a>
        </div>
        
        <div class="instructions">
            <b>📱 Hướng dẫn đưa ra màn hình chính & sử dụng Micro tốt nhất:</b>
            <ol>
                <li>Mở camera điện thoại quét mã QR hoặc truy cập link trên.</li>
                <li><span class="highlight">⚠️ Nếu quét bằng Zalo / Messenger:</span> Hãy bấm dấu 3 chấm góc trên bên phải &rarr; Chọn <b>"Mở bằng trình duyệt" (Safari hoặc Chrome)</b> để được cấp quyền Micro giọng nói.</li>
                <li>Trên <b>iPhone (Safari)</b>: Bấm icon Chia sẻ (hình vuông mũi tên lên) &rarr; Chọn <span class="highlight">"Thêm vào MH chính" (Add to Home Screen)</span>.</li>
                <li>Trên <b>Android (Chrome)</b>: Bấm menu 3 chấm &rarr; Chọn <span class="highlight">"Cài đặt ứng dụng"</span> hoặc <span class="highlight">"Thêm vào Màn hình chính"</span>.</li>
                <li>App sẽ xuất hiện icon riêng ngoài màn hình, mở toàn màn hình, hỗ trợ Camera quét thuốc và Mic tiếng Việt đầy đủ!</li>
            </ol>
        </div>
    </div>
</body>
</html>
"""
        with open(qr_html_path, "w", encoding="utf-8") as f:
            f.write(html_content)
            
        print("[3/3] Dang mo trang ma QR tren trinh duyet may tinh...")
        webbrowser.open(f"file://{os.path.abspath(qr_html_path)}")
        print("\n=> Ban chi can cam dien thoai len, bat 5G/4G va quet ma QR tren man hinh!")
        print("=> Giu cua so nay mo trong suot thoi gian su dung app tren dien thoai.\n")
        
        # Tiep tuc giu tien trinh va in log
        try:
            for line in iter(process.stdout.readline, ''):
                pass
        except KeyboardInterrupt:
            print("\nDang dung Cloudflare Tunnel...")
            process.terminate()
    else:
        print("[!] Khong lay duoc link Cloudflare Tunnel. Kiem tra lai ket noi mang.")
        process.terminate()

if __name__ == "__main__":
    main()

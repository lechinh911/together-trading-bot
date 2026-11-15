Together We Thrive — goi chay tren Windows VPS
================================================

Goi runtime toi gian: Python 3.13.17 (embed x64) + bot, khong can cai Git hay Python he thong.
Chi tao tin hieu / lenh ao. Khong dat lenh that, khong can API key Binance.

CANH BAO: Khi VPS bat bot, PHAI DUNG bot tren box/cloud truoc. Hai may cung luc se tranh
nhau cap nhat bot.json. Bot moi se TU CHOI push neu bot.json tren GitHub con moi (< 8 phut)
va den tu may khac. Neu can lay quyen ngay sau khi da dung may kia: set TBOT_FORCE_PUBLISH=1
roi start lai (hoac doi het cua so 8 phut).

Cai dat (mot lan)
-----------------
1. Giai nen goi vao mot thu muc (vi du C:\together_bot hoac D:\together_bot).
2. Chay install.bat (chuot phai > Run as administrator neu muon tat sleep cua Windows).
3. Nhap GitHub token (ky tu bi an). Token duoc luu o User environment variable
   GITHUB_TOKEN_TOGETHER — KHONG ghi ra file, KHONG dua vao git config.
   Token can quyen ghi file tren nha lechinh911/together-trading-bot (nhanh data).
4. install.bat tao Task Scheduler "TogetherWeThriveBot" (chay luc dang nhap va khi khoi
   dong, tu start lai 3 lan neu chet), roi start bot.

Lenh hang ngay
--------------
  start.bat     start bot (cua so hien tai) hoac dat lich da cai se giu bot chay
  stop.bat      dung bot
  status.bat    task, PID, generated_t7, trang thai WebSocket
  uninstall.bat dung bot, xoa task va xoa token khoi User env (giu file/data)

Sau reboot
----------
Task chay khi user dang nhap. Tren VPS nen bat tu dang nhap (auto-logon) de bot tu chay
sau moi lan khoi dong. Chay "khi chua logon" can tai khoan/mat khau Windows — goi nay
khong luu mat khau Windows.

Power / sleep
-------------
Neu install.bat chay quyen admin: tat sleep/hibernate. Neu khong, tu chay trong cmd Admin:
  powercfg /change standby-timeout-ac 0
  powercfg /change hibernate-timeout-ac 0

Kiem tra
--------
Doi bot warm xong (vai phut). status.bat hien generated_t7. Dashboard doc
https://raw.githubusercontent.com/lechinh911/together-trading-bot/data/bot.json
(moi 60 giay). Truong publisher_host cho biet may nao dang push.

Go di chuyen sang VPS khac
--------------------------
Copy nguyen thu muc goi. Tren may moi chay uninstall khong can. Chay install.bat, nhap
lai token. Dung bot tren may cu truoc.

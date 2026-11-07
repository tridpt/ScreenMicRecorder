# Quay màn hình + Micro

Ứng dụng Windows quay màn hình thành MP4, kèm micro, âm thanh đang phát từ máy tính và webcam nếu chọn. Không cần Xbox Game Bar hay tính năng quay màn hình tích hợp.

## Dùng bản đã đóng gói

1. Giải nén `ScreenMicRecorder-Windows.zip`, rồi mở `ScreenMicRecorder/ScreenMicRecorder.exe`.
2. Chọn màn hình. Nếu chỉ muốn quay một phần, nhấn **Chọn vùng quay**, kéo chuột tạo vùng rồi thả. Nhấn **Toàn màn hình** để bỏ vùng đã chọn.
3. Chọn micro. Trong **Tiếng máy tính**, chọn loa hoặc tai nghe đang phát để thu âm thanh máy; chọn **Không thu tiếng máy** nếu chỉ muốn thu micro.
4. Chọn webcam nếu muốn hình webcam nằm trong video, rồi chọn góc hiển thị.
5. Chọn thư mục lưu, nhấn **Bắt đầu quay**. Trong khi ghi, có thể nhấn **Tạm dừng**, **Tiếp tục**, rồi **Dừng & lưu**.

Phím tắt toàn hệ thống hiển thị ở cuối cửa sổ. Mặc định là **Ctrl+Alt+R** để bắt đầu, **Ctrl+Alt+P** để tạm dừng/tiếp tục, và **Ctrl+Alt+S** để dừng. Nếu phím đã bị ứng dụng khác dùng, ứng dụng tự chuyển sang tổ hợp có thêm **Shift** và hiển thị tổ hợp thực tế.

Video mặc định lưu vào `Videos/ScreenMicRecorder` trong tài khoản Windows. Các khoảng tạm dừng được bỏ khỏi video. Sau khi nhấn dừng, hãy đợi trạng thái **Đã lưu**; việc chèn webcam và ghép các đoạn có thể mất thêm thời gian. Ứng dụng không tải bản ghi lên mạng.

## Lưu ý thiết bị

- **Tiếng máy** dùng WASAPI loopback của thiết bị đầu ra đã chọn. Nếu đổi từ loa sang tai nghe trong lúc quay, hãy dừng rồi chọn lại thiết bị. Nếu không có âm thanh phát ra, đoạn tiếng máy sẽ im lặng; tiếng micro vẫn được ghi.
- Nếu micro hoặc webcam không xuất hiện, kiểm tra **Settings → Privacy & security → Microphone/Camera**, cho phép ứng dụng desktop dùng thiết bị, rồi nhấn **Làm mới**. Đóng ứng dụng khác đang chiếm webcam nếu cần.
- Nếu laptop quay chậm, chọn **720p** và **15 khung hình/giây**, hoặc tắt webcam.
- Nếu việc lưu thất bại, ứng dụng giữ các đoạn gốc trong thư mục tạm được nêu ở thông báo lỗi để bạn có thể khôi phục.

## Chạy mã nguồn

Cần Windows 10/11 và Python 3.10 trở lên. Tại thư mục dự án:

```powershell
python -m pip install -r requirements-build.txt
python -c "from static_ffmpeg.run import get_or_fetch_platform_executables_else_raise; get_or_fetch_platform_executables_else_raise()"
python app.py
```

Có thể đặt `ffmpeg.exe` trong thư mục `bin` cạnh mã nguồn hoặc cài FFmpeg vào `PATH`. FFmpeg cần hỗ trợ `gdigrab`, `dshow`, `libx264` và `aac`.

## Tạo bản chạy độc lập

```powershell
.\build.ps1
```

Tệp thực thi nằm trong `dist/ScreenMicRecorder/`; `dist/ScreenMicRecorder-Windows.zip` chứa toàn bộ bản để sao chép. Giữ nguyên cả thư mục `ScreenMicRecorder` sau khi giải nén vì `_internal` chứa FFmpeg và các thư viện cần thiết. Bản build chạy trên Windows 64-bit và không cần cài Python trên máy đích.

Giấy phép các thành phần kèm theo: `NOTICE-FFMPEG.txt` và `NOTICE-PYAUDIOWPATCH.txt`.

# 成片终检: 抽帧 → 空白扫描 → 联络表
$ff = (Get-Command ffmpeg -ErrorAction SilentlyContinue).Source
if (-not $ff) {
  $w = "$env:LOCALAPPDATA\Microsoft\WinGet\Packages"
  $d = Get-ChildItem $w -Directory | Where-Object Name -like 'Gyan.FFmpeg*' | Select-Object -First 1
  $ff = (Get-ChildItem (Join-Path $d.FullName 'bin') -Filter ffmpeg.exe).FullName
}
$dir = '.openbitfun\tmp\video\pv\final90'
Remove-Item $dir -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $dir | Out-Null
# 30 点: 每 3s 一点 + 全部关键节拍 (15.469 cut / 站点切换 / 面板 / steps / endcard) 加密
$times = @(0.5, 3, 6, 9, 12, 13.13, 14.5, 15.469, 15.7, 17, 18.75, 21, 24, 26.7, 28.1, 31, 34, 37.5, 40, 43, 46, 49, 51, 52.5, 55, 58, 61, 63.75, 67, 70, 73.1, 75, 77, 79.5, 82, 84.5, 86.5, 88.5)
for ($i = 0; $i -lt $times.Count; $i++) {
  & $ff -y -v error -ss $times[$i] -i '.openbitfun\tmp\video\pv\aigate-pv.mp4' -frames:v 1 (Join-Path $dir ("f{0:D2}.png" -f $i))
}
"抽帧 $($times.Count) 点完成"

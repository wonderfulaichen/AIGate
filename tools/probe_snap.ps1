# 探针: 拍指定时刻并拷出 (兼容绝对帧号: 帧名 = f{round(t*30)})
param([double[]]$Times, [string]$Tag)
$i = 0
foreach ($t in $Times) {
  node tools/capture_pv.js $t ($t + 0.034) --settle 2>&1 | Select-String 'pageerror|Error' | ForEach-Object { "  $_" }
  $f = 'f{0:D5}.jpg' -f [int]([math]::Round($t * 30))
  $src = ".openbitfun\tmp\video\pv\frames\$f"
  if (Test-Path $src) {
    Copy-Item $src ".openbitfun\tmp\video\pv\${Tag}$($t -replace '\.','_').jpg" -Force
    "probe t=$t OK ($f)"
  } else { "probe t=$t FAIL ($f 不存在)" }
  $i++
}

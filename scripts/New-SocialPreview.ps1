[CmdletBinding()]
param(
  [string]$OutputPath = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
$scriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
if ([string]::IsNullOrWhiteSpace($OutputPath)) {
  $OutputPath = Join-Path (Split-Path -Parent $scriptDirectory) 'docs\assets\social-preview.png'
}

function New-RoundedRectanglePath {
  param(
    [Parameter(Mandatory)][Drawing.RectangleF]$Rectangle,
    [Parameter(Mandatory)][single]$Radius
  )
  $diameter = $Radius * 2
  $path = [Drawing.Drawing2D.GraphicsPath]::new()
  $path.AddArc($Rectangle.Left, $Rectangle.Top, $diameter, $diameter, 180, 90)
  $path.AddArc($Rectangle.Right - $diameter, $Rectangle.Top, $diameter, $diameter, 270, 90)
  $path.AddArc($Rectangle.Right - $diameter, $Rectangle.Bottom - $diameter, $diameter, $diameter, 0, 90)
  $path.AddArc($Rectangle.Left, $Rectangle.Bottom - $diameter, $diameter, $diameter, 90, 90)
  $path.CloseFigure()
  return $path
}

$OutputPath = [IO.Path]::GetFullPath($OutputPath)
New-Item -ItemType Directory -Path (Split-Path -Parent $OutputPath) -Force | Out-Null
$bitmap = [Drawing.Bitmap]::new(1280, 640, [Drawing.Imaging.PixelFormat]::Format32bppArgb)
$graphics = [Drawing.Graphics]::FromImage($bitmap)
$graphics.SmoothingMode = [Drawing.Drawing2D.SmoothingMode]::AntiAlias
$graphics.TextRenderingHint = [Drawing.Text.TextRenderingHint]::AntiAliasGridFit

try {
  $canvas = [Drawing.Rectangle]::new(0, 0, 1280, 640)
  $background = [Drawing.Drawing2D.LinearGradientBrush]::new(
    $canvas,
    [Drawing.Color]::FromArgb(255, 10, 17, 31),
    [Drawing.Color]::FromArgb(255, 15, 39, 58),
    18
  )
  $graphics.FillRectangle($background, $canvas)
  $background.Dispose()

  $gridPen = [Drawing.Pen]::new([Drawing.Color]::FromArgb(24, 119, 211, 255), 1)
  for ($x = 0; $x -le 1280; $x += 64) { $graphics.DrawLine($gridPen, $x, 0, $x, 640) }
  for ($y = 0; $y -le 640; $y += 64) { $graphics.DrawLine($gridPen, 0, $y, 1280, $y) }
  $gridPen.Dispose()

  $terminalRect = [Drawing.RectangleF]::new(76, 72, 710, 496)
  $terminalPath = New-RoundedRectanglePath -Rectangle $terminalRect -Radius 24
  $terminalFill = [Drawing.SolidBrush]::new([Drawing.Color]::FromArgb(224, 8, 15, 28))
  $terminalStroke = [Drawing.Pen]::new([Drawing.Color]::FromArgb(110, 99, 210, 255), 2)
  $graphics.FillPath($terminalFill, $terminalPath)
  $graphics.DrawPath($terminalStroke, $terminalPath)
  $terminalFill.Dispose(); $terminalStroke.Dispose(); $terminalPath.Dispose()

  $dotColors = @(
    [Drawing.Color]::FromArgb(255, 255, 126, 107),
    [Drawing.Color]::FromArgb(255, 255, 202, 92),
    [Drawing.Color]::FromArgb(255, 94, 218, 165)
  )
  for ($i = 0; $i -lt 3; $i++) {
    $brush = [Drawing.SolidBrush]::new($dotColors[$i])
    $graphics.FillEllipse($brush, 108 + ($i * 28), 104, 13, 13)
    $brush.Dispose()
  }

  $promptFont = [Drawing.Font]::new('Consolas', 18, [Drawing.FontStyle]::Regular, [Drawing.GraphicsUnit]::Pixel)
  $promptBrush = [Drawing.SolidBrush]::new([Drawing.Color]::FromArgb(255, 107, 231, 194))
  $graphics.DrawString('PS> portable-codex setup', $promptFont, $promptBrush, 108, 148)
  $promptFont.Dispose(); $promptBrush.Dispose()

  $titleFont = [Drawing.Font]::new('Segoe UI Semibold', 49, [Drawing.FontStyle]::Bold, [Drawing.GraphicsUnit]::Pixel)
  $titleBrush = [Drawing.SolidBrush]::new([Drawing.Color]::FromArgb(255, 243, 249, 255))
  $graphics.DrawString('Codex CLI', $titleFont, $titleBrush, 106, 206)
  $graphics.DrawString('Portable Setup Kit', $titleFont, $titleBrush, 106, 265)
  $titleFont.Dispose(); $titleBrush.Dispose()

  $tagFont = [Drawing.Font]::new('Segoe UI', 22, [Drawing.FontStyle]::Regular, [Drawing.GraphicsUnit]::Pixel)
  $tagBrush = [Drawing.SolidBrush]::new([Drawing.Color]::FromArgb(255, 161, 189, 211))
  $graphics.DrawString('Reproduce a tested Codex workspace.', $tagFont, $tagBrush, 110, 345)
  $graphics.DrawString('Integrity, backups, and real rollback.', $tagFont, $tagBrush, 110, 377)
  $tagFont.Dispose(); $tagBrush.Dispose()

  $labels = @('VERIFY', 'BACK UP', 'ROLL BACK')
  for ($i = 0; $i -lt $labels.Count; $i++) {
    $chip = [Drawing.RectangleF]::new(108 + ($i * 190), 449, 164, 54)
    $chipPath = New-RoundedRectanglePath -Rectangle $chip -Radius 16
    $chipFill = [Drawing.SolidBrush]::new([Drawing.Color]::FromArgb(62, 91, 218, 255))
    $chipStroke = [Drawing.Pen]::new([Drawing.Color]::FromArgb(150, 102, 226, 255), 1.5)
    $graphics.FillPath($chipFill, $chipPath)
    $graphics.DrawPath($chipStroke, $chipPath)
    $chipFill.Dispose(); $chipStroke.Dispose(); $chipPath.Dispose()
    $chipFont = [Drawing.Font]::new('Segoe UI Semibold', 17, [Drawing.FontStyle]::Bold, [Drawing.GraphicsUnit]::Pixel)
    $chipBrush = [Drawing.SolidBrush]::new([Drawing.Color]::FromArgb(255, 210, 246, 255))
    $format = [Drawing.StringFormat]::new()
    $format.Alignment = [Drawing.StringAlignment]::Center
    $format.LineAlignment = [Drawing.StringAlignment]::Center
    $graphics.DrawString($labels[$i], $chipFont, $chipBrush, $chip, $format)
    $format.Dispose(); $chipFont.Dispose(); $chipBrush.Dispose()
  }

  $halo = [Drawing.SolidBrush]::new([Drawing.Color]::FromArgb(45, 69, 222, 255))
  $graphics.FillEllipse($halo, 850, 92, 350, 350)
  $halo.Dispose()

  $shieldPoints = [Drawing.PointF[]]@(
    [Drawing.PointF]::new(1025, 142),
    [Drawing.PointF]::new(1140, 188),
    [Drawing.PointF]::new(1122, 332),
    [Drawing.PointF]::new(1025, 404),
    [Drawing.PointF]::new(928, 332),
    [Drawing.PointF]::new(910, 188)
  )
  $shieldFill = [Drawing.SolidBrush]::new([Drawing.Color]::FromArgb(230, 20, 57, 77))
  $shieldStroke = [Drawing.Pen]::new([Drawing.Color]::FromArgb(255, 102, 232, 255), 6)
  $graphics.FillPolygon($shieldFill, $shieldPoints)
  $graphics.DrawPolygon($shieldStroke, $shieldPoints)
  $shieldFill.Dispose(); $shieldStroke.Dispose()

  $checkPen = [Drawing.Pen]::new([Drawing.Color]::FromArgb(255, 105, 235, 183), 18)
  $checkPen.StartCap = [Drawing.Drawing2D.LineCap]::Round
  $checkPen.EndCap = [Drawing.Drawing2D.LineCap]::Round
  $graphics.DrawLines($checkPen, [Drawing.PointF[]]@(
    [Drawing.PointF]::new(968, 272),
    [Drawing.PointF]::new(1010, 316),
    [Drawing.PointF]::new(1088, 232)
  ))
  $checkPen.Dispose()

  $arrowPen = [Drawing.Pen]::new([Drawing.Color]::FromArgb(255, 255, 191, 87), 12)
  $arrowPen.StartCap = [Drawing.Drawing2D.LineCap]::Round
  $arrowPen.EndCap = [Drawing.Drawing2D.LineCap]::Round
  $graphics.DrawArc($arrowPen, 925, 418, 200, 128, 22, 300)
  $graphics.DrawLines($arrowPen, [Drawing.PointF[]]@(
    [Drawing.PointF]::new(940, 438),
    [Drawing.PointF]::new(912, 462),
    [Drawing.PointF]::new(950, 473)
  ))
  $arrowPen.Dispose()

  $smallFont = [Drawing.Font]::new('Consolas', 17, [Drawing.FontStyle]::Regular, [Drawing.GraphicsUnit]::Pixel)
  $smallBrush = [Drawing.SolidBrush]::new([Drawing.Color]::FromArgb(255, 151, 190, 213))
  $graphics.DrawString('Windows  |  PowerShell  |  MIT', $smallFont, $smallBrush, 885, 570)
  $smallFont.Dispose(); $smallBrush.Dispose()

  $bitmap.Save($OutputPath, [Drawing.Imaging.ImageFormat]::Png)
} finally {
  $graphics.Dispose()
  $bitmap.Dispose()
}

$item = Get-Item -LiteralPath $OutputPath
Write-Output "SOCIAL_PREVIEW=PASS path=$($item.FullName) bytes=$($item.Length) size=1280x640"

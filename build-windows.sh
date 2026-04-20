#!/bin/bash
# ========================================
# Forensic Report System - Windows Package Build
# Run on macOS, produces Windows-ready zip
# ========================================

set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
DIST_DIR="$PROJECT_DIR/dist-forensic-report"
ZIP_NAME="ForensicReport_v1.0_Windows.zip"

echo "========================================"
echo "  Forensic Report System - Windows Build"
echo "========================================"
echo ""

# 1. Clean old build
rm -rf "$DIST_DIR"
mkdir -p "$DIST_DIR"

# 2. Build frontend
echo "[1/5] Building frontend..."
cd "$PROJECT_DIR/frontend"
npm run build
echo "      Frontend build done"

# 3. Copy backend code
echo "[2/5] Copying backend..."
mkdir -p "$DIST_DIR/backend/app"
cp -r "$PROJECT_DIR/backend/main.py" "$DIST_DIR/backend/"
cp -r "$PROJECT_DIR/backend/.env" "$DIST_DIR/backend/" 2>/dev/null || true
cp -r "$PROJECT_DIR/backend/requirements.txt" "$DIST_DIR/backend/"
cp -r "$PROJECT_DIR/backend/app/" "$DIST_DIR/backend/app/"

# Copy frontend build output to backend/static
echo "[3/5] Copying frontend static files..."
rm -rf "$DIST_DIR/backend/static"
cp -r "$PROJECT_DIR/frontend/dist" "$DIST_DIR/backend/static"

# 4. Copy startup scripts (English names, English content, CRLF line endings)
echo "[4/5] Copying startup scripts..."
cp "$PROJECT_DIR/start.bat" "$DIST_DIR/"
cp "$PROJECT_DIR/stop.bat" "$DIST_DIR/"
# Ensure CRLF line endings for Windows CMD
find "$DIST_DIR" -name "*.bat" -exec perl -pi -e 's/\r?\n/\r\n/' {} \;

# 5. Clean up unnecessary files
echo "[5/5] Cleaning up..."
find "$DIST_DIR" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
find "$DIST_DIR" -name "*.pyc" -delete 2>/dev/null || true
rm -f "$DIST_DIR/backend/forensic_report.db" 2>/dev/null || true
rm -rf "$DIST_DIR/backend/uploads" 2>/dev/null || true
rm -rf "$DIST_DIR/backend/logs" 2>/dev/null || true
rm -rf "$DIST_DIR/backend/reports" 2>/dev/null || true
rm -f "$DIST_DIR/backend/installed.flag" 2>/dev/null || true

# Package
echo ""
echo "Packaging..."
cd "$PROJECT_DIR"
rm -f "$ZIP_NAME"
cd "$DIST_DIR"
zip -r "../$ZIP_NAME" . -x "*.DS_Store"
cd ..

# Stats
SIZE=$(du -sh "$ZIP_NAME" | cut -f1)
echo ""
echo "========================================"
echo "  Build complete!"
echo "========================================"
echo "  File: $ZIP_NAME"
echo "  Size: $SIZE"
echo ""
echo "  Usage:"
echo "  1. Send zip to Windows user"
echo "  2. Extract to any folder (avoid Chinese/space in path)"
echo "  3. Double-click start.bat"
echo "  4. Open browser: http://localhost:8000"
echo "========================================"


import os
import json
import shutil
import tempfile
import traceback

import fontforge
from flask import Flask, request, jsonify, send_file, render_template

# app.pyと同じ場所にあるindex.htmlを直接読み込む
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_FILE = os.path.join(BASE_DIR, "index.html")

app = Flask(__name__, template_folder=BASE_DIR)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024


def save_upload(upload, directory, filename):
    if not upload or not upload.filename:
        raise ValueError("フォントファイルが選択されていません。")

    path = os.path.join(directory, filename)
    upload.save(path)

    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        raise ValueError("空のファイル、または保存できないファイルです。")

    return path


def glyph_map(font):
    result = {}
    for glyph in font.glyphs():
        cp = glyph.unicode
        if isinstance(cp, int) and 0 <= cp <= 0x10FFFF:
            result[cp] = glyph
    return result


def inspect_font(path):
    font = fontforge.open(path)
    try:
        glyphs = []

        for glyph in font.glyphs():
            cp = glyph.unicode
            if isinstance(cp, int) and 0 <= cp <= 0x10FFFF:
                glyphs.append({
                    "cp": cp,
                    "char": chr(cp),
                    "name": glyph.glyphname,
                    "width": int(glyph.width),
                })

        glyphs.sort(key=lambda item: item["cp"])

        return {
            "family": str(font.familyname or ""),
            "displayName": str(font.fullname or ""),
            "style": str(font.weight or "Regular"),
            "postScriptName": str(font.fontname or ""),
            "version": str(font.version or "1.000"),
            "glyphs": glyphs,
        }
    finally:
        font.close()


@app.get("/")
def index():
    # index.htmlはapp.pyと同じフォルダに置く
    if not os.path.isfile(INDEX_FILE):
        return (
            "index.htmlが見つかりません。app.pyと同じフォルダに置いてください。",
            404,
        )

    return app.send_static_file("index.html")


@app.post("/api/inspect")
def api_inspect():
    try:
        upload = request.files.get("font")

        with tempfile.TemporaryDirectory() as tmp:
            path = save_upload(upload, tmp, "input-font")
            data = inspect_font(path)

        return jsonify({"ok": True, "font": data})

    except Exception as exc:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/api/merge")
def api_merge():
    base = None
    source = None
    final_path = None

    try:
        upload_a = request.files.get("fontA")
        upload_b = request.files.get("fontB")

        if not upload_a or not upload_b:
            raise ValueError("フォントAとフォントBを両方選択してください。")

        family = request.form.get("family", "").strip()
        display_name = request.form.get("displayName", "").strip()
        style = request.form.get("style", "Regular").strip()
        ps_name = request.form.get("postScriptName", "").strip()
        version = request.form.get("version", "1.000").strip()
        output_format = request.form.get("format", "otf").lower()

        if output_format not in ("otf", "ttf"):
            raise ValueError("出力形式はOTFまたはTTFを選んでください。")

        try:
            replacements = json.loads(
                request.form.get("replacements", "[]")
            )
        except json.JSONDecodeError:
            raise ValueError("置換リストのデータが不正です。")

        if not isinstance(replacements, list):
            raise ValueError("置換リストは配列で指定してください。")

        with tempfile.TemporaryDirectory() as tmp:
            path_a = save_upload(upload_a, tmp, "fontA")
            path_b = save_upload(upload_b, tmp, "fontB")

            base = fontforge.open(path_a)
            source = fontforge.open(path_b)

            a_map = glyph_map(base)
            b_map = glyph_map(source)

            # AにないUnicode文字をBから補完
            added = 0

            for cp, src_glyph in b_map.items():
                if cp in a_map:
                    continue

                target = base.createChar(cp, src_glyph.glyphname)
                src_glyph.draw(target.glyphPen())
                target.width = src_glyph.width
                a_map[cp] = target
                added += 1

            # 置換前の幅を保存してから手動置換
            replaced = 0

            for item in replacements:
                if not isinstance(item, dict):
                    raise ValueError("置換設定の形式が不正です。")

                try:
                    target_cp = int(item["targetCp"])
                    source_cp = int(item["sourceCp"])
                except (KeyError, TypeError, ValueError):
                    raise ValueError("置換する文字コードが不正です。")

                if not (0 <= target_cp <= 0x10FFFF):
                    raise ValueError("置換先の文字コードが不正です。")

                if not (0 <= source_cp <= 0x10FFFF):
                    raise ValueError("置換元の文字コードが不正です。")

                if source_cp not in b_map:
                    raise ValueError(
                        f"フォントBにU+{source_cp:04X}がありません。"
                    )

                src_glyph = b_map[source_cp]
                existed_before = target_cp in a_map

                if existed_before:
                    target = a_map[target_cp]
                    old_width = target.width
                else:
                    target = base.createChar(
                        target_cp, f"uni{target_cp:04X}"
                    )
                    a_map[target_cp] = target
                    old_width = src_glyph.width

                src_glyph.draw(target.glyphPen())
                target.width = old_width
                replaced += 1

            # メタデータ
            if family:
                base.familyname = family
            if display_name:
                base.fullname = display_name
            if style:
                base.weight = style
            if ps_name:
                base.fontname = ps_name
            if version:
                base.version = version

            output_path = os.path.join(
                tmp, f"merged-font.{output_format}"
            )

            base.generate(output_path)

            if not os.path.isfile(output_path):
                raise RuntimeError("フォントファイルが生成されませんでした。")

            if os.path.getsize(output_path) == 0:
                raise RuntimeError("生成されたフォントファイルが空です。")

            # 生成したファイルを開いて検証
            check = fontforge.open(output_path)
            try:
                output_count = len(glyph_map(check))
            finally:
                check.close()

            if output_count == 0:
                raise RuntimeError("生成されたフォントに文字がありません。")

            # 一時フォルダ終了後もダウンロードできる場所へコピー
            fd, final_path = tempfile.mkstemp(
                suffix=f".{output_format}"
            )
            os.close(fd)
            shutil.copyfile(output_path, final_path)

        response = send_file(
            final_path,
            as_attachment=True,
            download_name=f"merged-font.{output_format}",
            mimetype=(
                "font/otf" if output_format == "otf"
                else "font/ttf"
            ),
        )

        response.headers["X-Glyphs-Added"] = str(added)
        response.headers["X-Glyphs-Replaced"] = str(replaced)
        response.headers["X-Glyphs-Total"] = str(output_count)

        @response.call_on_close
        def cleanup():
            try:
                if final_path and os.path.exists(final_path):
                    os.remove(final_path)
            except OSError:
                pass

        return response

    except Exception as exc:
        traceback.print_exc()

        if final_path and os.path.exists(final_path):
            try:
                os.remove(final_path)
            except OSError:
                pass

        return jsonify({"ok": False, "error": str(exc)}), 400

    finally:
        if base is not None:
            try:
                base.close()
            except Exception:
                pass

        if source is not None:
            try:
                source.close()
            except Exception:
                pass


if __name__ == "__main__":
    # PC上で動かす場合の設定。
    # iPadからアクセスするなら、PCのLAN内IPアドレスで開く。
    app.run(host="0.0.0.0", port=5000, debug=False)

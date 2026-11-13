import os
import re
import html
import logging
import tempfile
from pathlib import Path

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ConversationHandler, ContextTypes, filters
)

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Image, PageBreak, KeepTogether
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

try:
    import arabic_reshaper
    from bidi.algorithm import get_display
except ImportError:
    arabic_reshaper = None
    get_display = None

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN")
FONT_PATH = os.getenv("ARABIC_FONT_PATH", "fonts/DejaVuSans.ttf")

BOOK_NAME, COVER, SCENES = range(3)
MAX_SCENES = 150
MAX_TEXT_LENGTH = 5000
MAX_IMAGE_BYTES = 8 * 1024 * 1024


def shape_arabic(text: str) -> str:
    """Prepare Arabic text for ReportLab's basic left-to-right paragraph engine."""
    if arabic_reshaper and get_display:
        return get_display(arabic_reshaper.reshape(text))
    return text


def safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]+', "_", name).strip()
    return (name[:70] or "my_book") + ".pdf"


async def download_photo(message, context, folder: Path) -> str:
    photo = message.photo[-1]
    tg_file = await context.bot.get_file(photo.file_id)
    destination = folder / f"image_{len(list(folder.glob('image_*')))+1}.jpg"
    await tg_file.download_to_drive(custom_path=str(destination))
    if destination.stat().st_size > MAX_IMAGE_BYTES:
        destination.unlink(missing_ok=True)
        raise ValueError("حجم الصورة أكبر من الحد المسموح (8 ميجابايت).")
    return str(destination)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "مرحباً بك في صانع الكتب 📚\n\n"
        "لإنشاء كتاب جديد أرسل /newbook\n"
        "لإلغاء العملية الحالية أرسل /cancel\n"
        "للمساعدة أرسل /help"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "طريقة الاستخدام:\n"
        "1) /newbook لبدء كتاب.\n"
        "2) أرسل اسم الكتاب.\n"
        "3) أرسل صورة الغلاف.\n"
        "4) أرسل مشاهدك: نصوصاً وصوراً بالترتيب المطلوب.\n"
        "5) أرسل «تم» لإنشاء PDF.\n\n"
        "يمكنك إرسال صورة مع تعليق؛ سيُضاف التعليق بعدها مباشرة.\n"
        "أرسل /cancel لإلغاء الكتاب."
    )


async def new_book(update: Update, context: ContextTypes.DEFAULT_TYPE):
    old = context.user_data.get("book")
    if old and old.get("folder"):
        import shutil
        shutil.rmtree(old["folder"], ignore_errors=True)

    folder = Path(tempfile.mkdtemp(prefix=f"book_{update.effective_user.id}_"))
    context.user_data["book"] = {
        "name": "",
        "cover": None,
        "scenes": [],
        "folder": str(folder),
    }
    await update.message.reply_text("📖 لنبدأ كتاباً جديداً!\nأرسل اسم الكتاب:")
    return BOOK_NAME


async def get_book_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = (update.message.text or "").strip()
    if not name:
        await update.message.reply_text("اسم الكتاب لا يمكن أن يكون فارغاً. أرسله مرة أخرى:")
        return BOOK_NAME
    if len(name) > 120:
        await update.message.reply_text("اجعل اسم الكتاب 120 حرفاً أو أقل.")
        return BOOK_NAME

    context.user_data["book"]["name"] = name
    await update.message.reply_text("جميل! أرسل صورة غلاف الكتاب 📕")
    return COVER


async def get_cover(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("أحتاج صورة للغلاف. أرسلها كصورة وليس كملف.")
        return COVER

    book = context.user_data["book"]
    try:
        book["cover"] = await download_photo(
            update.message, context, Path(book["folder"])
        )
    except Exception as exc:
        await update.message.reply_text(f"تعذر حفظ الصورة: {exc}")
        return COVER

    await update.message.reply_text(
        "تم حفظ الغلاف ✅\n\n"
        "أرسل مشاهد الكتاب الآن، صورة أو نصاً أو صورة مع تعليق.\n"
        "سيُحفظ كل شيء بنفس ترتيب وصوله.\n\n"
        "عندما تنتهي أرسل: تم"
    )
    return SCENES


def add_text_scene(book, text):
    text = text.strip()
    if text:
        if len(text) > MAX_TEXT_LENGTH:
            raise ValueError(f"النص طويل جداً. الحد الأقصى {MAX_TEXT_LENGTH} حرف.")
        book["scenes"].append({"type": "text", "content": text})


async def get_scenes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    book = context.user_data.get("book")

    if message.text and message.text.strip().lower() in ("تم", "done", "/done"):
        if not book["scenes"]:
            await message.reply_text("لم تضف أي مشاهد بعد. أرسل نصاً أو صورة أولاً.")
            return SCENES
        await message.reply_text("⏳ جاري تجهيز ملف PDF...")
        try:
            pdf_path = build_pdf(book)
            with open(pdf_path, "rb") as pdf_file:
                await message.reply_document(
                    document=pdf_file,
                    filename=safe_filename(book["name"]),
                    caption=f"🎉 اكتمل كتاب «{book['name']}»"
                )
        except Exception:
            logger.exception("PDF generation failed")
            await message.reply_text(
                "حدث خطأ أثناء إنشاء PDF. تأكد من وجود ملف الخط العربي "
                "fonts/DejaVuSans.ttf ثم حاول مجدداً."
            )
            return SCENES
        finally:
            import shutil
            shutil.rmtree(book["folder"], ignore_errors=True)

        context.user_data.pop("book", None)
        return ConversationHandler.END

    if len(book["scenes"]) >= MAX_SCENES:
        await message.reply_text(f"وصلت للحد الأقصى: {MAX_SCENES} مشهداً.")
        return SCENES

    try:
        if message.photo:
            image_path = await download_photo(
                message, context, Path(book["folder"])
            )
            book["scenes"].append({"type": "image", "content": image_path})
            if message.caption:
                add_text_scene(book, message.caption)
            await message.reply_text("تم حفظ الصورة" + (" والتعليق" if message.caption else "") + " ✅")
        elif message.text:
            add_text_scene(book, message.text)
            await message.reply_text("تم حفظ النص ✅")
        else:
            await message.reply_text("أرسل صورة أو نصاً فقط.")
    except ValueError as exc:
        await message.reply_text(str(exc))
    except Exception:
        logger.exception("Scene handling failed")
        await message.reply_text("لم أستطع حفظ هذا المشهد. حاول مرة أخرى.")

    return SCENES


def build_pdf(book) -> str:
    folder = Path(book["folder"])
    output = folder / "book.pdf"

    if not Path(FONT_PATH).exists():
        raise FileNotFoundError(f"Arabic font not found: {FONT_PATH}")

    try:
        pdfmetrics.registerFont(TTFont("BookArabic", FONT_PATH))
    except Exception:
        # Re-registration can happen when multiple users create books.
        pass

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "BookTitle", parent=styles["Title"], fontName="BookArabic",
        fontSize=23, leading=32, alignment=TA_CENTER, textColor=colors.HexColor("#263238")
    )
    body_style = ParagraphStyle(
        "BookBody", parent=styles["BodyText"], fontName="BookArabic",
        fontSize=14, leading=24, alignment=TA_RIGHT, spaceAfter=10
    )

    story = [
        Spacer(1, 1.5 * cm),
        Paragraph(html.escape(shape_arabic(book["name"])), title_style),
        Spacer(1, 1 * cm),
    ]

    cover = Image(book["cover"])
    cover._restrictSize(15 * cm, 19 * cm)
    story.extend([cover, PageBreak()])

    for scene in book["scenes"]:
        if scene["type"] == "text":
            safe_text = html.escape(shape_arabic(scene["content"])).replace("\n", "<br/>")
            story.append(Paragraph(safe_text, body_style))
            story.append(Spacer(1, 0.35 * cm))
        else:
            img = Image(scene["content"])
            img._restrictSize(16 * cm, 20 * cm)
            story.append(KeepTogether([img, Spacer(1, 0.5 * cm)]))

    doc = SimpleDocTemplate(
        str(output), pagesize=A4,
        rightMargin=2 * cm, leftMargin=2 * cm,
        topMargin=2 * cm, bottomMargin=2 * cm,
        title=book["name"]
    )
    doc.build(story)
    return str(output)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    book = context.user_data.pop("book", None)
    if book and book.get("folder"):
        import shutil
        shutil.rmtree(book["folder"], ignore_errors=True)
    await update.message.reply_text("تم إلغاء إنشاء الكتاب وحذف ملفاته المؤقتة. ❌")
    return ConversationHandler.END


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Unhandled error", exc_info=context.error)


def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is missing. Set it in your hosting environment variables.")

    app = Application.builder().token(TOKEN).build()

    conversation = ConversationHandler(
        entry_points=[CommandHandler("newbook", new_book)],
        states={
            BOOK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_book_name)],
            COVER: [MessageHandler(filters.PHOTO, get_cover)],
            SCENES: [MessageHandler(filters.ALL & ~filters.COMMAND, get_scenes)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(conversation)
    app.add_error_handler(error_handler)

    logger.info("Book maker bot is running")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

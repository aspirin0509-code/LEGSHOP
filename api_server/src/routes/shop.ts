import { Router } from "express";
import { Pool } from "pg";

const router = Router();
const pool = new Pool({ connectionString: process.env["DATABASE_URL"], max: 40 });

router.get("/orders", async (req, res) => {
  try {
    const result = await pool.query(
      `SELECT id, fio, phone, status, is_bonus, created_at FROM orders ORDER BY created_at DESC`
    );
    res.json(result.rows);
  } catch (err) {
    res.status(500).json({ error: "DB error" });
  }
});

// Создание заявки из Mini App (до оплаты — чтобы PDF из банка принимался сразу)
router.post("/orders", async (req, res) => {
  try {
    const { user_id, fio, phone, referred_by } = req.body;
    const uid = Number(user_id) || 0;
    if (!String(fio || '').trim() || !String(phone || '').trim()) {
      return res.status(400).json({ error: "fio и phone обязательны" });
    }
    // uid=0 допустим — заказ будет найден по номеру телефона когда пользователь пришлёт чек

    const phoneClean = String(phone).trim();
    const referredBy = referred_by ? Number(referred_by) : null;

    // Если у пользователя уже есть активная заявка без чека — возвращаем её
    const existing = await pool.query(
      uid > 0
        ? `SELECT id FROM orders WHERE user_id = $1 AND photo_id IS NULL ORDER BY id DESC LIMIT 1`
        : `SELECT id FROM orders WHERE phone = $1 AND photo_id IS NULL ORDER BY id DESC LIMIT 1`,
      [uid > 0 ? String(uid) : phoneClean]
    );
    if (existing.rows.length > 0) {
      return res.json({ order_id: existing.rows[0].id, reused: true });
    }

    const result = await pool.query(
      `INSERT INTO orders (user_id, fio, phone, status, referred_by) VALUES ($1, $2, $3, 'в модерации', $4) RETURNING id`,
      [uid, String(fio).trim(), phoneClean, referredBy]
    );
    res.json({ order_id: result.rows[0].id, reused: false });
  } catch (err) {
    console.error("POST /orders error:", err);
    res.status(500).json({ error: "DB error" });
  }
});

// Находит ОДИН заказ пользователя по Telegram user_id (для обратной совместимости)
router.get("/my-order", async (req, res) => {
  try {
    const userId = Number(req.query.user_id);
    if (!userId || userId <= 0) return res.status(400).json({ error: "user_id required" });

    const result = await pool.query(
      `SELECT id FROM orders WHERE user_id = $1 AND is_bonus = FALSE ORDER BY id DESC LIMIT 1`,
      [String(userId)]
    );
    if (result.rows.length === 0) return res.status(404).json({ error: "not found" });
    res.json({ order_id: result.rows[0].id });
  } catch (err) {
    res.status(500).json({ error: "DB error" });
  }
});

// Все заказы пользователя — по Telegram user_id или номеру телефона
router.get("/my-orders", async (req, res) => {
  try {
    const userId = Number(req.query.user_id);
    const phone = String(req.query.phone || '').trim();

    let rows: any[] = [];

    if (userId > 0) {
      const r = await pool.query(
        `SELECT id, fio, phone, status, is_bonus, created_at
         FROM orders WHERE user_id = $1 ORDER BY id DESC`,
        [String(userId)]
      );
      rows = r.rows;
    }

    // Если по user_id ничего нет (или user_id не передан) — ищем по телефону
    if (rows.length === 0 && phone.length >= 6) {
      const clean = phone.replace(/\D/g, '');
      const r = await pool.query(
        `SELECT id, fio, phone, status, is_bonus, created_at
         FROM orders WHERE regexp_replace(phone, '\\D', '', 'g') LIKE $1 ORDER BY id DESC`,
        [`%${clean}%`]
      );
      rows = r.rows;
    }

    if (rows.length === 0) return res.status(404).json({ error: "not found" });
    res.json(rows);
  } catch (err) {
    res.status(500).json({ error: "DB error" });
  }
});

// Реферальная статистика по order_id пригласившего
router.get("/referrals", async (req, res) => {
  try {
    const orderId = Number(req.query.order_id);
    if (!orderId) return res.status(400).json({ error: "order_id required" });

    const result = await pool.query(
      `SELECT COUNT(*) as count FROM orders WHERE referred_by = $1 AND status = 'заказ принят' AND is_bonus = FALSE`,
      [orderId]
    );
    const count = parseInt(result.rows[0].count, 10);
    const needed = 10;
    const bonuses = Math.floor(count / needed);
    const progress = count % needed;

    res.json({ count, needed, progress, bonuses });
  } catch (err) {
    res.status(500).json({ error: "DB error" });
  }
});

router.get("/stats", async (req, res) => {
  try {
    const result = await pool.query(
      `SELECT COUNT(*) as confirmed FROM orders WHERE status = 'заказ принят' AND is_bonus = FALSE`
    );
    const confirmed = parseInt(result.rows[0].confirmed, 10);
    const total = 2000;
    const remaining = Math.max(0, total - confirmed);
    res.json({ total, confirmed, remaining });
  } catch (err) {
    res.status(500).json({ error: "DB error" });
  }
});

export default router;

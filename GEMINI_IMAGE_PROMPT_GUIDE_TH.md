# คู่มือสั่งสร้างภาพด้วย Gemini (ผ่าน MCP-ChatBridge)

กฎข้อเดียวที่สำคัญที่สุด: **ต้องสั่งด้วย prompt ที่ละเอียดเสมอ** prompt สั้นๆ ได้ภาพที่ "ใช้ได้"
แต่ไม่ใช่ภาพที่ต้องการ

## 1. ทำไมต้องละเอียด (หลักฐานจากการสร้างจริง 2026-10-31)

เรื่องเดียวกัน (ภาพหัวเว็บโทน haloendeavor.com) สร้างด้วย Gemini สามรอบ:

| รอบ | prompt | ผลที่ได้ |
|---|---|---|
| 1 | สั้น ระบุสีเข้ม + หัวข้อภาพ | องค์ประกอบเล็กอยู่กลางภาพ ขอบเป็นแผงสีเรียบ ดูโล่งและแบน |
| 2 | สั้น เหมือนรอบ 1 แต่ฐานสว่าง | ตกที่เดิม: อยู่กลางภาพ ขอบซ้ายขวาเป็นแผงว่าง |
| 3 | **ละเอียด** ระบุรูปแบบ ฉาก มุมกล้อง วัสดุ แสง ชั้นความลึก สี ข้อห้าม | เต็มเฟรมจริง เป็นงานเรนเดอร์ 3 มิติ มีรายละเอียดและความตื้นลึก |

Gemini เติมส่วนที่คุณไม่ได้พูดด้วยค่าเฉลี่ยของมันเอง ยิ่งเว้นช่องว่างไว้มาก
ผลยิ่งสุ่มและยิ่งจืด

หมายเหตุ: คู่มือนี้ยังไม่ได้เทียบกับ ChatGPT ด้วย prompt เดียวกัน (โควตาฟรีของ ChatGPT หมดในวันที่ทดสอบ)

## 2. โครงของ prompt (7 ส่วน)

เขียนเป็นภาษาอังกฤษ (ได้ผลสม่ำเสมอกว่า) ขึ้นต้นด้วย `Generate an image:` เสมอ
เพราะ bridge ไม่เขียน prompt ใหม่ให้ และ Gemini ต้องรู้ว่านี่คือคำขอสร้างภาพ

| # | ส่วน | ต้องระบุอะไร | ตัวอย่าง |
|---|---|---|---|
| 1 | **Format** | สัดส่วน, เต็มเฟรมหรือไม่, ไม่มีกรอบ/อุปกรณ์ | `ultra-wide 16:9, FULL-BLEED, no borders, no mockup device` |
| 2 | **Scene** | สิ่งที่อยู่ในภาพ จำนวน ตำแหน่ง ความสัมพันธ์ | `halo ring above center-right, about 12 node cubes linked by thin traces` |
| 3 | **Composition / camera** | มุมมอง ระยะ ชั้นหน้า-กลาง-หลัง ความตื้นลึก | `isometric, high three-quarter angle, foreground blurred, midground sharp` |
| 4 | **Materials / lighting** | วัสดุ ทิศแสง การเรืองแสง เงา | `translucent glass torus, lavender rim light, soft ambient occlusion` |
| 5 | **Palette** | สีเป็น **hex** พร้อมบอกว่าสีไหนใช้กับอะไร | `navy #0f172a for slabs; indigo #4f46e5 for glowing traces; amber #f6b94d only on one indicator` |
| 6 | **Style / mood** | สไตล์อ้างอิง อารมณ์ ระดับความคม | `clean developer-docs aesthetic merged with high-end 3D render, calm, precise` |
| 7 | **Strict negatives** | สิ่งที่ห้ามมี | `no text, no letters, no logos, no people, no watermark` |

## 3. เทมเพลต

```text
Generate an image: <one-sentence purpose, e.g. "a premium hero banner illustration for X">.
Format: <ratio>, FULL-BLEED composition that fills the entire frame (no empty side panels, no borders, no frame, no mockup device).

SCENE: <what is in the picture, how many, where, how they relate>.

COMPOSITION / CAMERA: <viewpoint, distance, foreground/midground/background, depth of field>.

MATERIALS / LIGHTING: <materials, light direction, glow, shadows, reflections>.

PALETTE (strict): <hex> for <part>; <hex> for <part>; <hex> only for <small accent>; no other hues.

STYLE: <style references, mood, sharpness>.

STRICT NEGATIVES: no text, no letters, no numbers, no logos, no watermarks, no people, <anything else you do not want>.
```

## 4. ตัวอย่างจริงที่ใช้ได้ผล (รอบ 3)

```text
Generate an image: a premium, highly detailed hero banner illustration for "Halo Endeavor", a developer-focused local AI agent platform. Format: ultra-wide 16:9, FULL-BLEED composition that fills the entire frame edge to edge (no empty side panels, no borders, no frame, no mockup device).

SCENE: an isometric 3D-rendered "agent control floor" seen from a high three-quarter angle. A large softly glowing translucent halo ring (a thick glass torus with a luminous lavender rim light) hovers above the center-right of the scene, casting a soft circular light pool onto the surface below. Under it, a layered platform of rounded-corner glass and matte-navy slabs carries a dense but orderly network: about 12 rounded node cubes of different sizes connected by thin right-angle circuit traces that glow faintly as data flows along them. Include small floating UI-like cards (blank, no readable text) at different depths, and a few thin dotted orbit arcs around the ring. In the lower-left foreground, one slightly larger dark-navy "core" module with a single amber indicator light. Depth of field: foreground slightly blurred, midground tack-sharp, background fading into soft haze.

PALETTE (strict): deep navy #0f172a and #1e293b for base and slabs; indigo #4f46e5 and blue #2563eb for glowing traces and node tops; soft lavender #9e8cff for the halo glow; one small warm amber #f6b94d only on the core's indicator; light slate #e2e8f0 for the thinnest highlights. No other hues.

STYLE: clean, precise, modern developer-documentation aesthetic merged with high-end 3D product render, soft global illumination, subtle ambient occlusion, crisp edges, gentle bloom on emissive parts, 4K sharpness. Calm, confident, technical mood.

STRICT NEGATIVES: no text, no letters, no numbers, no logos, no watermarks, no people, no faces, no robots, no cartoon style, no neon rainbow colors, no cluttered noise.
```

## 5. วิธีเรียก

```text
ask_gemini(
  action="generate_image",
  prompt="Generate an image: ...",   # ละเอียดตามหัวข้อ 2-3
  max_images=1,
  save_dir="",                        # ว่าง = ~/Pictures/MCP-ChatBridge/gemini
  timeout_seconds=300
)
```

- ใช้เวลาประมาณ 25-35 วินาที ห้ามเรียกซ้ำระหว่างรอ
- ผลสำเร็จ: `ok=true`, `images[].path` เป็นไฟล์ที่เซฟแล้ว ถ้า `source="full_download"` คือไฟล์เต็มขนาด
  (ราว 2752x1536) ถ้าเป็น `displayed` คือขนาดที่หน้าเว็บแสดง
- `status="no_image"`: Gemini ไม่ส่งรูป (ปฏิเสธ โควตา หรือถ้อยคำ) ห้ามส่งซ้ำอัตโนมัติ ให้ปรับ prompt แล้วขออนุญาตก่อน
- **เปิดดูภาพด้วยเครื่องมืออ่านรูปทุกครั้ง** ก่อนบอกว่างานเสร็จ แล้วเทียบกับรายการใน prompt

### แก้ภาพเดิม (image-to-image)

แนบรูปด้วย `attach_images=["/absolute/path.png"]` แล้วระบุให้ชัดว่า **อะไรคงไว้ อะไรเปลี่ยน**:

```text
Generate an image: use the attached illustration as the base and redraw it as a calm snowy night with a full moon, keeping the same fox and the same composition.
```

รูปที่แนบจะถูกอัปโหลดไป Google ส่งเฉพาะรูปที่ผู้ใช้อนุญาต

## 6. แก้อาการที่เจอบ่อย

| อาการ | ต้นเหตุ | ใส่เพิ่มใน prompt |
|---|---|---|
| องค์ประกอบเล็กอยู่กลางภาพ ขอบเป็นแผงว่าง | ไม่ได้สั่งเต็มเฟรม | `FULL-BLEED ... no empty side panels, no borders` |
| ภาพแบนไม่มีมิติ | ไม่ได้ระบุมุมกล้อง/ชั้นความลึก | `isometric, three-quarter angle, foreground blurred, midground sharp` |
| สีเพี้ยน เทาหม่น | ระบุสีลอยๆ ไม่บอกว่าใช้กับส่วนไหน | `<hex> for the TOP FACES of the cubes` และ `all surfaces equally dark` |
| มุมหนึ่งสว่างเกินโทนทั้งภาพ | แสงไม่ถูกกำหนด | `no bright white areas; only a faint cool glow in the upper-right corner` |
| มีตัวอักษรหรือโลโก้แทรก | ไม่ได้ห้าม | `no text, no letters, no numbers, no logos, no watermarks` |
| ได้ตัวละคร/แบรนด์ที่รู้จัก | อ้างถึงของมีลิขสิทธิ์ | ห้ามอ้างตัวละครหรือแบรนด์จริง ให้บรรยายตัวละครต้นฉบับแทน |
| ได้หลายรูปทั้งที่ต้องการรูปเดียว | `max_images` มากกว่า 1 | `max_images=1` |

## 7. ข้อควรระวัง

- Prompt ไปที่เว็บ Gemini ของ Google: **ห้ามใส่ข้อมูลลับ** รหัสผ่าน หรือข้อมูลส่วนตัว
- โควตารูปของบัญชีฟรีมีจำกัด อย่าสั่งทดสอบเล่น และอย่าวนส่ง prompt เดิมซ้ำ
- ภาพที่สร้างเก็บนอกรีโป (`~/Pictures/MCP-ChatBridge/gemini/`) และถูก `.gitignore` ไม่ขึ้น GitHub
- ใช้ Gemini สร้างรูปเป็นหลัก ส่วน ChatGPT ใช้เมื่อระบุชื่อเท่านั้น (โควตาฟรีน้อยกว่ามาก)

## 8. เช็คลิสต์ก่อนส่ง

- [ ] ขึ้นต้นด้วย `Generate an image:`
- [ ] ระบุสัดส่วนและ FULL-BLEED
- [ ] บรรยายฉาก จำนวน ตำแหน่ง
- [ ] ระบุมุมกล้องและชั้นความลึก
- [ ] ระบุวัสดุและแสง
- [ ] สีเป็น hex พร้อมบอกว่าใช้กับส่วนไหน
- [ ] มีรายการข้อห้าม (ข้อความ โลโก้ คน)
- [ ] `max_images=1`

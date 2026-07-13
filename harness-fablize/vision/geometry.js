// vision/geometry.js
//
// 純粋なブラウザ内 JS（依存なし・node 不要）。ロードすると window.__fablizeGeometry
// を定義する。視覚的な合否判断を DOM の幾何情報（getBoundingClientRect ベース）
// から数値で決定するためのアサーションランナー。
//
// 座標系: 常に画面座標（viewport 座標、CSS px、y は下向き正）。
//
// 重要な設計原則（意味論バグは最悪のバグ）:
//   - runAssertions() は例外を絶対に外へ投げない。個々のアサーションの評価が
//     失敗したら、そのアサーションを fail として message にエラー内容を積む。
//   - セレクタが1件もマッチしない → そのアサーションは fail
//     （"selector not found" 系メッセージ、決して黙って pass にしない）。
//   - 複数マッチ → 最初の要素を使い、message に件数を注記する。
//   - id が重複したアサーションは2件目以降を fail にする（意味論バグの温床
//     になりやすいため、こっそり上書きさせない）。
//   - 未知の type / 必須引数欠落は fail（"分からないものは pass にしない"）。
(function (global) {
  'use strict';

  function bboxOf(el) {
    var r = el.getBoundingClientRect();
    return {
      x: r.left,
      y: r.top,
      w: r.width,
      h: r.height,
      cx: r.left + r.width / 2,
      cy: r.top + r.height / 2,
      top: r.top,
      bottom: r.bottom,
      left: r.left,
      right: r.right,
    };
  }

  // セレクタから最初の要素を取得。0件マッチや不正セレクタは note にエラー
  // 文言を積んで el:null を返す（呼び出し側は el が null なら即 fail する）。
  function selectFirst(selector) {
    var list;
    try {
      list = document.querySelectorAll(selector);
    } catch (e) {
      return { el: null, count: 0, note: 'invalid selector: ' + selector + ' (' + (e && e.message ? e.message : e) + ')' };
    }
    if (!list || list.length === 0) {
      return { el: null, count: 0, note: 'selector not found: ' + selector };
    }
    var note = list.length > 1 ? ('matched ' + list.length + ' elements, using the first: ' + selector) : null;
    return { el: list[0], count: list.length, note: note };
  }

  function selectBBox(selector) {
    var found = selectFirst(selector);
    if (!found.el) {
      return { bbox: null, note: found.note };
    }
    return { bbox: bboxOf(found.el), note: found.note, el: found.el };
  }

  function clamp(v, lo, hi) {
    return Math.max(lo, Math.min(hi, v));
  }

  // 点からbboxまでの距離。attached=true なら内部は距離0、false(boundary)なら
  // 内部でも最寄り境界までの距離を返す（常に非負）。
  function pointToRectDistance(px, py, rect, attached) {
    if (attached) {
      var cx = clamp(px, rect.left, rect.right);
      var cy = clamp(py, rect.top, rect.bottom);
      return Math.hypot(px - cx, py - cy);
    }
    var inside = px >= rect.left && px <= rect.right && py >= rect.top && py <= rect.bottom;
    if (inside) {
      return Math.min(px - rect.left, rect.right - px, py - rect.top, rect.bottom - py);
    }
    var ncx = clamp(px, rect.left, rect.right);
    var ncy = clamp(py, rect.top, rect.bottom);
    return Math.hypot(px - ncx, py - ncy);
  }

  // <line>/<polyline>/<polygon>/<path> の start/end 端点を画面座標(px)で取得。
  // getScreenCTM() を通すことで viewBox のスケール・transform・入れ子 <g> を
  // 正しく反映する。
  function getEndpointScreenPoint(el, end) {
    var tag = el.tagName ? el.tagName.toLowerCase() : '';
    var svgOwner = el.ownerSVGElement || (el instanceof SVGSVGElement ? el : null);
    if (!svgOwner) {
      throw new Error('endpoint_near/arrow_direction: element is not inside an <svg>: <' + tag + '>');
    }

    var localPt;
    if (tag === 'line') {
      if (end === 'start') {
        localPt = { x: parseFloat(el.getAttribute('x1')), y: parseFloat(el.getAttribute('y1')) };
      } else {
        localPt = { x: parseFloat(el.getAttribute('x2')), y: parseFloat(el.getAttribute('y2')) };
      }
    } else if (tag === 'polyline' || tag === 'polygon') {
      var pts = el.points;
      if (!pts || pts.numberOfItems === 0) {
        throw new Error('endpoint_near/arrow_direction: <' + tag + '> has no points');
      }
      var p = end === 'start' ? pts.getItem(0) : pts.getItem(pts.numberOfItems - 1);
      localPt = { x: p.x, y: p.y };
    } else if (tag === 'path') {
      if (typeof el.getTotalLength !== 'function') {
        throw new Error('endpoint_near/arrow_direction: <path> does not support getTotalLength in this environment');
      }
      var total = el.getTotalLength();
      var svgPt = end === 'start' ? el.getPointAtLength(0) : el.getPointAtLength(total);
      localPt = { x: svgPt.x, y: svgPt.y };
    } else {
      throw new Error('endpoint_near/arrow_direction: unsupported element type <' + tag + '> (line/polyline/polygon/path のみ対応)');
    }

    var ctm = el.getScreenCTM();
    if (!ctm) {
      throw new Error('endpoint_near/arrow_direction: getScreenCTM() returned null for <' + tag + '> (非表示要素の可能性)');
    }
    var svgPoint = svgOwner.createSVGPoint();
    svgPoint.x = localPt.x;
    svgPoint.y = localPt.y;
    var screenPt = svgPoint.matrixTransform(ctm);
    return { x: screenPt.x, y: screenPt.y };
  }

  // 要素の簡潔な記述（タグ名 + id + class）。visible_at_center の遮蔽物
  // メッセージに使う。
  function describeElement(el) {
    if (!el) return 'null';
    var tag = el.tagName ? el.tagName.toLowerCase() : '?';
    var id = el.id ? ('#' + el.id) : '';
    var klass = (typeof el.getAttribute === 'function' && el.getAttribute('class'))
      ? ('.' + el.getAttribute('class').replace(/\s+/g, '.'))
      : '';
    return '<' + tag + id + klass + '>';
  }

  // computed style の marker-start/marker-end 値（例: 'url("#id")'）が指す
  // <marker> 要素を解決し、その orient 属性の生値を返す。orient 属性が省略
  // されている場合の SVG の既定値は "0"（固定角度・パス方向に依存しない）
  // であり、これも呼び出し側では「auto でも auto-start-reverse でもない」
  // 扱いとして返す（黙って "auto" とみなさない）。marker 要素が解決できない
  // 場合は null を返す。
  function resolveMarkerOrient(markerRefValue) {
    if (!markerRefValue) return null;
    var m = /url\(\s*["']?#([^"')]+)["']?\s*\)/.exec(markerRefValue);
    if (!m) return null;
    var markerEl = document.getElementById(m[1]);
    if (!markerEl) return null;
    var orient = markerEl.getAttribute('orient');
    return orient === null ? '0' : orient.trim();
  }

  // --- forall 型アサーション用ヘルパ ------------------------------------
  // 文書全体（セレクタで単一要素を特定できない生成物）を対象にする
  // アサーション群（text_present / no_overlap_text_leaves /
  // no_horizontal_overflow / max_page_height / all_text_visible）が共有する。

  // 可視テキストの集計から除外するタグ（コード・スタイル定義であり画面上の
  // テキストではない）。
  var TEXT_SKIP_TAGS = { SCRIPT: true, STYLE: true, TEMPLATE: true, NOSCRIPT: true };

  // 要素自身または祖先のいずれかが display:none / visibility:hidden なら
  // true（黙って可視扱いにしない）。
  function isElementHidden(el) {
    var node = el;
    while (node && node.nodeType === 1) {
      var cs;
      try {
        cs = getComputedStyle(node);
      } catch (e) {
        return false;
      }
      if (!cs) return false;
      if (cs.display === 'none' || cs.visibility === 'hidden') return true;
      node = node.parentElement;
    }
    return false;
  }

  // 要素の「直下」テキストノードのみを連結したもの（子孫要素のテキストは
  // 含まない）。空白は単一スペースに畳んで前後をトリムする。
  function directText(el) {
    var s = '';
    for (var i = 0; i < el.childNodes.length; i++) {
      var cn = el.childNodes[i];
      if (cn.nodeType === 3) s += cn.nodeValue;
    }
    return s.replace(/\s+/g, ' ').trim();
  }

  // 文書内の可視テキストノードを列挙する（display:none等の非表示・
  // script/style/template/noscript 配下・空白のみのノードは除く）。
  // text_present が対象にする「レンダリング後の可視テキスト」。
  function collectVisibleTextNodes(root) {
    var scope = root || document.body || document.documentElement;
    var results = [];
    if (!scope || typeof document.createTreeWalker !== 'function') return results;
    var walker = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT, null, false);
    var node;
    while ((node = walker.nextNode())) {
      var text = node.nodeValue;
      if (!text || /^\s*$/.test(text)) continue;
      var parent = node.parentElement;
      if (!parent) continue;
      if (TEXT_SKIP_TAGS[parent.tagName]) continue;
      if (isElementHidden(parent)) continue;
      results.push(text);
    }
    return results;
  }

  // 「テキスト葉」= 空白以外の直下テキストノードを持つ、可視な要素。
  // no_overlap_text_leaves / no_horizontal_overflow / all_text_visible が
  // 共有する列挙ヘルパ（セレクタで単一要素を特定できない生成物の採点に使う）。
  function collectTextLeaves(root) {
    var scope = root || document.body || document.documentElement;
    var results = [];
    if (!scope) return results;
    var candidates = [scope];
    var descendants = scope.querySelectorAll('*');
    for (var i = 0; i < descendants.length; i++) candidates.push(descendants[i]);
    for (var j = 0; j < candidates.length; j++) {
      var el = candidates[j];
      if (!el.tagName || TEXT_SKIP_TAGS[el.tagName]) continue;
      var hasDirectText = false;
      for (var c = 0; c < el.childNodes.length; c++) {
        var cn = el.childNodes[c];
        if (cn.nodeType === 3 && cn.nodeValue && !/^\s*$/.test(cn.nodeValue)) {
          hasDirectText = true;
          break;
        }
      }
      if (!hasDirectText) continue;
      if (isElementHidden(el)) continue;
      results.push(el);
    }
    return results;
  }

  // テキスト葉1件の簡潔な記述（タグ・テキスト先頭20字・座標）。
  // no_overlap_text_leaves の actual/message に使う。
  function describeTextLeaf(el, bbox) {
    var b = bbox || bboxOf(el);
    var tag = el.tagName ? el.tagName.toLowerCase() : '?';
    var text = directText(el);
    var snippet = text.length > 20 ? text.slice(0, 20) + '…' : text;
    return '<' + tag + '>"' + snippet + '"@(' + b.left.toFixed(1) + ',' + b.top.toFixed(1) + ' ' + b.w.toFixed(1) + 'x' + b.h.toFixed(1) + ')';
  }

  // visible_at_center と all_text_visible が共有する5点プローブ判定。
  // 要素 bbox の中心 + 四半点4箇所（中心と各辺中点の中間、計5点）で
  // document.elementFromPoint を呼び、いずれかの点で命中した要素が
  // 対象要素自身・その子孫・またはその祖先（対象要素を包む <g> 等）であれば
  // OK。5点すべて別要素（遮蔽物）が命中したら完全遮蔽。
  function probeElementVisibility(target) {
    var box = bboxOf(target);
    var points = [
      { label: 'center', x: box.cx, y: box.cy },
      { label: 'top-quarter', x: box.cx, y: (box.cy + box.top) / 2 },
      { label: 'bottom-quarter', x: box.cx, y: (box.cy + box.bottom) / 2 },
      { label: 'left-quarter', x: (box.cx + box.left) / 2, y: box.cy },
      { label: 'right-quarter', x: (box.cx + box.right) / 2, y: box.cy },
    ];
    var anyHit = false;
    var descs = [];
    for (var i = 0; i < points.length; i++) {
      var p = points[i];
      var hitEl = document.elementFromPoint(p.x, p.y);
      var coord = '(' + p.x.toFixed(1) + ',' + p.y.toFixed(1) + ')';
      if (!hitEl) {
        descs.push(p.label + '=' + coord + ' -> null (viewport外 or 要素なし)');
        continue;
      }
      var isSelf = hitEl === target;
      var isDescendant = !isSelf && typeof target.contains === 'function' && target.contains(hitEl);
      var isAncestor = !isSelf && !isDescendant && typeof hitEl.contains === 'function' && hitEl.contains(target);
      if (isSelf || isDescendant || isAncestor) {
        anyHit = true;
        descs.push(p.label + '=' + coord + ' -> ' + describeElement(hitEl) + ' (OK: self/descendant/ancestor-group)');
      } else {
        descs.push(p.label + '=' + coord + ' -> occluded by ' + describeElement(hitEl));
      }
    }
    return { anyHit: anyHit, descs: descs };
  }

  var BBOX_PROPS = ['x', 'y', 'w', 'h', 'cx', 'cy', 'top', 'bottom', 'left', 'right'];

  function resolveOperand(spec) {
    if (typeof spec === 'number') {
      return { value: spec, note: null };
    }
    if (!spec || typeof spec !== 'object' || !spec.selector || !spec.prop) {
      throw new Error('compare: operand must be a number or {selector, prop}');
    }
    if (BBOX_PROPS.indexOf(spec.prop) === -1) {
      throw new Error('compare: unknown prop "' + spec.prop + '" (allowed: ' + BBOX_PROPS.join(', ') + ')');
    }
    var found = selectBBox(spec.selector);
    if (!found.bbox) {
      throw new Error(found.note);
    }
    return { value: found.bbox[spec.prop], note: found.note };
  }

  // --- アサーション種別ハンドラ ---------------------------------------
  // 各ハンドラは { required: [...], run(a) } の形。run() は
  // { pass, actual, expected, message } を返す。run() 内で投げた例外は
  // 呼び出し側(evalAssertion)が fail に変換するので、ここでは投げてよい。

  var HANDLERS = {
    exists: {
      required: ['selector'],
      run: function (a) {
        var found = selectFirst(a.selector);
        if (!found.el) {
          return { pass: false, actual: 0, expected: '>=1', message: found.note };
        }
        return {
          pass: true,
          actual: found.count,
          expected: '>=1',
          message: found.note || ('matched ' + found.count + ' element(s)'),
        };
      },
    },

    touches: {
      required: ['a', 'b'],
      run: function (a) {
        var eps = typeof a.eps === 'number' ? a.eps : 0;
        var A = selectBBox(a.a);
        if (!A.bbox) throw new Error(A.note);
        var B = selectBBox(a.b);
        if (!B.bbox) throw new Error(B.note);
        var gapX = Math.max(0, B.bbox.left - A.bbox.right, A.bbox.left - B.bbox.right);
        var gapY = Math.max(0, B.bbox.top - A.bbox.bottom, A.bbox.top - B.bbox.bottom);
        var gap = Math.hypot(gapX, gapY);
        return {
          pass: gap <= eps,
          actual: gap,
          expected: '<= ' + eps,
          message: 'gap=' + gap.toFixed(2) + 'px (eps=' + eps + ')',
        };
      },
    },

    no_overlap: {
      required: ['selectors'],
      run: function (a) {
        var eps = typeof a.eps === 'number' ? a.eps : 0;
        if (!Array.isArray(a.selectors) || a.selectors.length < 2) {
          throw new Error('no_overlap: selectors must be an array with at least 2 entries');
        }
        var boxes = [];
        for (var i = 0; i < a.selectors.length; i++) {
          var found = selectBBox(a.selectors[i]);
          if (!found.bbox) throw new Error(found.note);
          boxes.push({ sel: a.selectors[i], bbox: found.bbox });
        }
        var worst = 0;
        var worstPair = null;
        for (var m = 0; m < boxes.length; m++) {
          for (var n = m + 1; n < boxes.length; n++) {
            var ba = boxes[m].bbox, bb = boxes[n].bbox;
            var ix = Math.min(ba.right, bb.right) - Math.max(ba.left, bb.left);
            var iy = Math.min(ba.bottom, bb.bottom) - Math.max(ba.top, bb.top);
            var depth = ix > 0 && iy > 0 ? Math.min(ix, iy) : 0;
            if (depth > worst) {
              worst = depth;
              worstPair = [boxes[m].sel, boxes[n].sel];
            }
          }
        }
        return {
          pass: worst <= eps,
          actual: worst,
          expected: '<= ' + eps,
          message: worst > eps
            ? 'overlap=' + worst.toFixed(2) + 'px between ' + worstPair[0] + ' and ' + worstPair[1] + ' (eps=' + eps + ')'
            : 'no overlap exceeding eps=' + eps,
        };
      },
    },

    contained_in: {
      required: ['inner', 'outer'],
      run: function (a) {
        var eps = typeof a.eps === 'number' ? a.eps : 0;
        var inner = selectBBox(a.inner);
        if (!inner.bbox) throw new Error(inner.note);
        var outer = selectBBox(a.outer);
        if (!outer.bbox) throw new Error(outer.note);
        var i = inner.bbox, o = outer.bbox;
        var violation = Math.max(
          o.left - i.left,
          i.right - o.right,
          o.top - i.top,
          i.bottom - o.bottom
        );
        return {
          pass: violation <= eps,
          actual: violation,
          expected: '<= ' + eps,
          message: 'max boundary violation=' + violation.toFixed(2) + 'px (eps=' + eps + ', 負値=余裕あり)',
        };
      },
    },

    relative_position: {
      required: ['of', 'to', 'position'],
      run: function (a) {
        var valid = ['above', 'below', 'left', 'right', 'top-left', 'top-right', 'bottom-left', 'bottom-right'];
        if (valid.indexOf(a.position) === -1) {
          throw new Error('relative_position: unknown position "' + a.position + '" (allowed: ' + valid.join(', ') + ')');
        }
        var of = selectBBox(a.of);
        if (!of.bbox) throw new Error(of.note);
        var to = selectBBox(a.to);
        if (!to.bbox) throw new Error(to.note);
        var O = of.bbox, T = to.bbox;
        var checks = {
          above: O.cy < T.cy,
          below: O.cy > T.cy,
          left: O.cx < T.cx,
          right: O.cx > T.cx,
          'top-left': O.cx < T.cx && O.cy < T.cy,
          'top-right': O.cx > T.cx && O.cy < T.cy,
          'bottom-left': O.cx < T.cx && O.cy > T.cy,
          'bottom-right': O.cx > T.cx && O.cy > T.cy,
        };
        var pass = checks[a.position];
        return {
          pass: pass,
          actual: { cx_of: O.cx, cy_of: O.cy, cx_to: T.cx, cy_to: T.cy },
          expected: a.position,
          message: 'of center=(' + O.cx.toFixed(1) + ',' + O.cy.toFixed(1) + ') to center=(' + T.cx.toFixed(1) + ',' + T.cy.toFixed(1) + ') vs "' + a.position + '"',
        };
      },
    },

    // 要素の累積変換（getScreenCTM）の行列式 det=a*d-b*c を見て鏡映
    // （左右/上下反転）を検出する。det<0 は鏡映、det>0 は非鏡映。
    // 注意（意味論の要）: 180度回転は a<0 かつ d<0 になるが、
    // det=(-1)*(-1)-0*0=1>0 で pass する — 回転は鏡映ではないため、これは
    // 正しい挙動（バグではない）。
    // v0 の制約: SVG のグラフィック要素（getScreenCTM を持つ）のみ対応。
    // HTML 要素は getScreenCTM を持たないため、黙って pass にはせず
    // 明確な fail メッセージ（unsupported element type）を返す。
    no_mirror: {
      required: ['selector'],
      run: function (a) {
        var found = selectFirst(a.selector);
        if (!found.el) throw new Error(found.note);
        var el = found.el;
        if (typeof el.getScreenCTM !== 'function') {
          return {
            pass: false,
            actual: null,
            expected: 'det > 0',
            message: 'no_mirror: unsupported element type <' + (el.tagName ? el.tagName.toLowerCase() : '?') +
              '> (getScreenCTM 非対応。v0 は SVG グラフィック要素のみ対応、HTML 要素は未対応)',
          };
        }
        var ctm = el.getScreenCTM();
        if (!ctm) {
          throw new Error('no_mirror: getScreenCTM() returned null for selector "' + a.selector + '" (非表示要素の可能性)');
        }
        var det = ctm.a * ctm.d - ctm.b * ctm.c;
        return {
          pass: det > 0,
          actual: { a: ctm.a, b: ctm.b, c: ctm.c, d: ctm.d, det: det },
          expected: '> 0',
          message: 'matrix(a=' + ctm.a.toFixed(4) + ', b=' + ctm.b.toFixed(4) + ', c=' + ctm.c.toFixed(4) +
            ', d=' + ctm.d.toFixed(4) + ') det=' + det.toFixed(4) + (det > 0 ? ' (鏡映なし)' : ' (鏡映を検出)'),
        };
      },
    },

    // 要素 bbox の中心 + 四半点4箇所（中心と各辺中点の中間、計5点）で
    // document.elementFromPoint を呼び、いずれかの点で命中した要素が
    // 対象要素自身・その子孫・またはその祖先（対象要素を包むグループ等）
    // であれば pass。5点すべて別要素（遮蔽物）が命中したら fail。
    // 既知の限界（README 参照）:
    //   - fill="none" の stroke のみ要素は中心点が自分に当たらないため
    //     誤 fail しうる（塗りのある要素にのみ使うこと）。
    //   - opacity<1 の半透明な要素も「遮蔽」と判定される。
    //   - viewport 外の要素は elementFromPoint が null を返すため fail する。
    visible_at_center: {
      required: ['selector'],
      run: function (a) {
        var found = selectFirst(a.selector);
        if (!found.el) throw new Error(found.note);
        var probe = probeElementVisibility(found.el);
        return {
          pass: probe.anyHit,
          actual: probe.descs,
          expected: '>=1 of 5 points hits self/descendant/ancestor-group',
          message: probe.descs.join(' | '),
        };
      },
    },

    // 文書全体の可視テキスト（display:none 等の非表示部分を除く）に対象
    // 文字列が min_count 回以上（非重複カウント）出現するか。セレクタで
    // 単一要素を特定できない生成物（緩い仕様から生成した文書）の採点用。
    //
    // 意味論バグ対策（2026-07-12）: 素朴に「テキストノードを文書順に \n 連結
    // して indexOf」だけだと、視覚的には連続した1文字列（例:
    // "99.97%"）でも DOM 上ノードが分かれていれば（例:
    // <div>99.97<span class="unit">%</span></div> のような「数値+単位を
    // 別要素にする」ごく一般的な意匠）ノード境界の区切り文字で分断され、
    // 見た目は完全に正しいのに不在判定される。同様に「428 ms」のように
    // 単位の前に空白を1つ入れる一般的な書式も、探索文字列が空白なしだと
    // 素朴な完全一致では見つからない。
    //
    // 対策として、(1) 従来どおりのノード境界保持版（sepJoined、ノード間に
    // \n を挟む。無関係な隣接テキストが偶然連結して誤ヒットしないための
    // 保守的な既定）と、(2) 空白を丸ごと畳んだ「詰め」版（tight。全ノードを
    // 区切りなしで連結し、ノード内外の空白を除去したものを探索文字列側も
    // 同様に空白除去してから比較）の2通りで照合し、どちらか一方でも
    // min_count に達すれば pass にする（count は両者の大きい方を採用。
    // 加算はしない＝同じ出現を2回カウントして水増ししない）。これにより
    // 「数値と単位を別要素/空白区切りにする」という構造非依存で正当な意匠を
    // 誤って fail にしない。
    text_present: {
      required: ['text'],
      run: function (a) {
        if (typeof a.text !== 'string' || a.text.length === 0) {
          throw new Error('text_present: text must be a non-empty string');
        }
        var minCount = typeof a.min_count === 'number' ? a.min_count : 1;
        var texts = collectVisibleTextNodes();

        function countOccurrences(haystack, needle) {
          var count = 0;
          var idx = 0;
          while (true) {
            var found = haystack.indexOf(needle, idx);
            if (found === -1) break;
            count++;
            idx = found + needle.length;
          }
          return count;
        }

        var sepJoined = texts.join('\n');
        var countSep = countOccurrences(sepJoined, a.text);

        var tightHaystack = texts.join('').replace(/\s+/g, '');
        var tightNeedle = a.text.replace(/\s+/g, '');
        var countTight = tightNeedle.length > 0 ? countOccurrences(tightHaystack, tightNeedle) : 0;

        var count = Math.max(countSep, countTight);
        return {
          pass: count >= minCount,
          actual: count,
          expected: '>= ' + minCount,
          message: 'text ' + JSON.stringify(a.text) + ' occurred ' + count + ' time(s) in visible text (min_count=' + minCount + '; node-boundary-preserving match=' + countSep + ', whitespace/node-boundary-insensitive match=' + countTight + ')',
        };
      },
    },

    // 全テキスト葉のペアについて、包含関係（親子入れ子）のペアを除外した上で
    // 交差矩形の短軸貫入深さが eps を超えたら fail。id 固定のセレクタで拾え
    // ない構造（エージェントの構造選択に依存する生成物）でのレイアウト事故
    // （テキスト同士の重なり）検出用。
    no_overlap_text_leaves: {
      required: ['eps'],
      run: function (a) {
        var eps = a.eps;
        var excludeSelector = a.exclude_selector || null;
        var leaves = collectTextLeaves();
        if (excludeSelector) {
          leaves = leaves.filter(function (el) {
            try {
              return !el.closest(excludeSelector);
            } catch (e) {
              throw new Error('no_overlap_text_leaves: invalid exclude_selector: ' + excludeSelector + ' (' + (e && e.message ? e.message : e) + ')');
            }
          });
        }
        var items = leaves.map(function (el) {
          return { el: el, bbox: bboxOf(el) };
        });
        var FUZZ = 0.01; // 浮動小数の丸め用の内部許容誤差（ユーザー指定epsとは別）
        function contains(o, i) {
          return o.left <= i.left + FUZZ && o.right >= i.right - FUZZ && o.top <= i.top + FUZZ && o.bottom >= i.bottom - FUZZ;
        }
        var violations = [];
        for (var m = 0; m < items.length; m++) {
          for (var n = m + 1; n < items.length; n++) {
            var A = items[m], B = items[n];
            if (contains(A.bbox, B.bbox) || contains(B.bbox, A.bbox)) continue; // 親子入れ子は除外
            var ix = Math.min(A.bbox.right, B.bbox.right) - Math.max(A.bbox.left, B.bbox.left);
            var iy = Math.min(A.bbox.bottom, B.bbox.bottom) - Math.max(A.bbox.top, B.bbox.top);
            if (ix > 0 && iy > 0) {
              var depth = Math.min(ix, iy);
              if (depth > eps) {
                violations.push({
                  depth: depth,
                  desc: describeTextLeaf(A.el, A.bbox) + ' overlaps ' + describeTextLeaf(B.el, B.bbox) + ' depth=' + depth.toFixed(2) + 'px',
                });
              }
            }
          }
        }
        var shown = violations.slice(0, 10);
        var pass = violations.length === 0;
        return {
          pass: pass,
          actual: shown.map(function (v) { return v.desc; }),
          expected: 'no violating pair (eps=' + eps + ')',
          message: pass
            ? ('no overlapping text leaves exceeding eps=' + eps + ' (' + items.length + ' text leaf(ves) checked)')
            : (violations.length + ' violating pair(s) found (eps=' + eps + ')' + (violations.length > 10 ? '; 先頭10件のみ actual に表示' : '')),
        };
      },
    },

    // document.documentElement.scrollWidth と全テキスト葉の right が、
    // どちらも viewport幅+eps 以内に収まっているか（横方向のはみ出し検出）。
    no_horizontal_overflow: {
      required: ['eps'],
      run: function (a) {
        var eps = a.eps;
        var viewportWidth = window.innerWidth;
        var scrollWidth = document.documentElement.scrollWidth;
        var docOverflow = scrollWidth > viewportWidth + eps;
        var leaves = collectTextLeaves();
        var overflowingLeaves = [];
        for (var i = 0; i < leaves.length; i++) {
          var b = bboxOf(leaves[i]);
          if (b.right > viewportWidth + eps) {
            overflowingLeaves.push(describeTextLeaf(leaves[i], b));
          }
        }
        var pass = !docOverflow && overflowingLeaves.length === 0;
        return {
          pass: pass,
          actual: {
            scrollWidth: scrollWidth,
            viewportWidth: viewportWidth,
            overflowing_text_leaves: overflowingLeaves.slice(0, 10),
          },
          expected: 'scrollWidth <= viewportWidth+eps かつ 全テキスト葉のright <= viewportWidth+eps',
          message: pass
            ? ('no horizontal overflow (scrollWidth=' + scrollWidth + 'px, viewportWidth=' + viewportWidth + 'px, eps=' + eps + ')')
            : ('horizontal overflow: scrollWidth=' + scrollWidth + 'px viewportWidth=' + viewportWidth + 'px eps=' + eps +
               (overflowingLeaves.length ? '; overflowing text leaves: ' + overflowingLeaves.slice(0, 10).join(' | ') : '')),
        };
      },
    },

    // document.documentElement.scrollHeight が max+eps 以内か（縦方向の
    // ページ高さ上限検出）。
    max_page_height: {
      required: ['max', 'eps'],
      run: function (a) {
        var max = a.max, eps = a.eps;
        var scrollHeight = document.documentElement.scrollHeight;
        var pass = scrollHeight <= max + eps;
        return {
          pass: pass,
          actual: scrollHeight,
          expected: '<= ' + max + ' (eps=' + eps + ')',
          message: 'document.documentElement.scrollHeight=' + scrollHeight + 'px vs max=' + max + 'px (eps=' + eps + ')',
        };
      },
    },

    // 全テキスト葉（max_elements 件まで）に visible_at_center と同じ5点
    // プローブを適用し、1つでも完全遮蔽ならfail。id 固定のセレクタで拾えない
    // 構造での z順事故（不透明要素による後描画の遮蔽）検出用。
    all_text_visible: {
      required: [],
      run: function (a) {
        var maxElements = typeof a.max_elements === 'number' ? a.max_elements : 200;
        var leaves = collectTextLeaves();
        var total = leaves.length;
        var checked = leaves.slice(0, maxElements);
        var skipped = total - checked.length;
        var occluded = [];
        for (var i = 0; i < checked.length; i++) {
          var el = checked[i];
          var probe = probeElementVisibility(el);
          if (!probe.anyHit) {
            occluded.push(describeTextLeaf(el) + ' | ' + probe.descs.join(' | '));
          }
        }
        var pass = occluded.length === 0;
        var messageParts = [checked.length + '/' + total + ' 件のテキスト葉を検査'];
        if (skipped > 0) {
          // 黙って全数検査したふりをしない: max_elements 超過分は検査せず
          // 明示する。
          messageParts.push('max_elements(' + maxElements + ')超過のため' + skipped + '件は未検査');
        }
        if (!pass) {
          messageParts.push(occluded.length + '件が完全遮蔽');
        }
        return {
          pass: pass,
          actual: occluded,
          expected: '0 occluded text leaves (checked=' + checked.length + '/' + total + ')',
          message: messageParts.join('; '),
        };
      },
    },

    endpoint_near: {
      required: ['selector', 'end', 'target', 'eps'],
      run: function (a) {
        if (a.end !== 'start' && a.end !== 'end') {
          throw new Error('endpoint_near: end must be "start" or "end"');
        }
        var where = a.where || 'attached';
        if (where !== 'attached' && where !== 'boundary') {
          throw new Error('endpoint_near: where must be "attached" or "boundary"');
        }
        var found = selectFirst(a.selector);
        if (!found.el) throw new Error(found.note);
        var pt = getEndpointScreenPoint(found.el, a.end);
        var target = selectBBox(a.target);
        if (!target.bbox) throw new Error(target.note);
        var dist = pointToRectDistance(pt.x, pt.y, target.bbox, where === 'attached');
        return {
          pass: dist <= a.eps,
          actual: dist,
          expected: '<= ' + a.eps,
          message: 'endpoint(' + a.end + ')=(' + pt.x.toFixed(1) + ',' + pt.y.toFixed(1) + ') dist=' + dist.toFixed(2) + 'px to ' + a.target + ' (where=' + where + ', eps=' + a.eps + ')',
        };
      },
    },

    // 注意（意味論の注記）: 既定（`head` 省略）では start→end の変位ベクトル
    // ＝ <line> なら x1,y1→x2,y2 の「座標記述順」で向きを判定する。これは
    // *描画された矢頭の向き* とは独立な量である — 別要素（polygon や
    // marker-start/marker-end）で矢頭を描く idiom では、線の座標をどちら向き
    // に書くかは描き手の自由（画素は不変）なので、座標順だけでは視覚上の
    // 矢印の向きを正しく判定できないことがある（README 参照）。視覚上の
    // 矢頭位置に基づいて判定したい場合は `head` を指定すること:
    //   head: "start" | "end"  … <marker-start>/<marker-end> 相当。start/end
    //                            のどちらが矢頭側かを明示する（既定は "end"
    //                            — 従来の座標順ベースの挙動と完全互換）。
    //   head: "<selector>"     … 矢頭を描く別要素（polygon 等）のセレクタ。
    //                            その要素の中心に近い方の端点を「矢頭側」と
    //                            みなし、そこへ向かうベクトルで判定する
    //                            （line/path の座標記述順に依存しない）。
    // endpoint_near の end:"start"/"end" は <line>/<path> の座標記述順に
    // 依存する（描き手がどちら向きに座標を書くかは画素を変えずに自由に
    // 選べる）。「コネクタが A と B の両方に接続していればよく、どちら向きに
    // 座標を書いたかは問わない」場合はこちらの順序非依存版を使う。
    endpoints_touch: {
      required: ['selector', 'a', 'b'],
      run: function (a) {
        var eps = typeof a.eps === 'number' ? a.eps : 0;
        var where = a.where || 'attached';
        if (where !== 'attached' && where !== 'boundary') {
          throw new Error('endpoints_touch: where must be "attached" or "boundary"');
        }
        var found = selectFirst(a.selector);
        if (!found.el) throw new Error(found.note);
        var start = getEndpointScreenPoint(found.el, 'start');
        var end = getEndpointScreenPoint(found.el, 'end');
        var A = selectBBox(a.a);
        if (!A.bbox) throw new Error(A.note);
        var B = selectBBox(a.b);
        if (!B.bbox) throw new Error(B.note);
        var attached = where === 'attached';
        var dStartA = pointToRectDistance(start.x, start.y, A.bbox, attached);
        var dEndB = pointToRectDistance(end.x, end.y, B.bbox, attached);
        var dStartB = pointToRectDistance(start.x, start.y, B.bbox, attached);
        var dEndA = pointToRectDistance(end.x, end.y, A.bbox, attached);
        var forward = Math.max(dStartA, dEndB); // start~a, end~b の場合の最悪距離
        var reverse = Math.max(dStartB, dEndA); // start~b, end~a の場合の最悪距離
        var best = Math.min(forward, reverse);
        var orientation = forward <= reverse
          ? ('start~' + a.a + ', end~' + a.b)
          : ('start~' + a.b + ', end~' + a.a);
        return {
          pass: best <= eps,
          actual: best,
          expected: '<= ' + eps,
          message: 'best-orientation(' + orientation + ') max-dist=' + best.toFixed(2) + 'px (where=' + where + ', eps=' + eps + ')',
        };
      },
    },

    arrow_direction: {
      required: ['selector', 'direction'],
      run: function (a) {
        var valid = ['up', 'down', 'left', 'right'];
        if (valid.indexOf(a.direction) === -1) {
          throw new Error('arrow_direction: unknown direction "' + a.direction + '" (allowed: ' + valid.join(', ') + ')');
        }
        var found = selectFirst(a.selector);
        if (!found.el) throw new Error(found.note);
        var start = getEndpointScreenPoint(found.el, 'start');
        var end = getEndpointScreenPoint(found.el, 'end');

        var head = a.head === undefined || a.head === null ? 'end' : a.head;
        var from, to, headNote;
        if (head === 'end') {
          from = start; to = end;
        } else if (head === 'start') {
          from = end; to = start;
        } else if (head === 'auto') {
          // computed style の marker-end / marker-start を見て矢頭側を自動
          // 判定する。生成物の採点にはこの head:"auto" を使うことを推奨する
          // （README 参照）。
          //   - marker-end が none 以外（marker-start の有無に関わらず）
          //     → head:"end" と同じ挙動。
          //   - marker-end が none かつ marker-start が none 以外
          //     → marker-start が指す <marker> の orient 属性を見て判定する
          //       （下記参照。orient を見ずに一律 head:"start" とみなすと、
          //       非慣用の marker-start + orient="auto"（auto-start-reverse
          //       ではない）で視覚と逆向きに判定してしまう footgun がある）。
          //   - 両方 none、または両方とも none 以外（両端に矢頭）
          //     → head:"end" にフォールバックし、その旨を message に残す
          //     （黙って判定しない）。
          var csAuto = getComputedStyle(found.el);
          var markerEndVal = csAuto.markerEnd;
          var markerStartVal = csAuto.markerStart;
          var endHas = !!markerEndVal && markerEndVal !== 'none';
          var startHas = !!markerStartVal && markerStartVal !== 'none';
          if (endHas && !startHas) {
            from = start; to = end;
            headNote = 'auto: marker-end=' + markerEndVal + ' を検出 → head="end" として判定; ';
          } else if (!endHas && startHas) {
            // marker-start は orient の値によって視覚上の向きが逆になる:
            //   - orient="auto-start-reverse"（慣用形）: パス方向を180度反転
            //     して向けるため、矢頭は end→start 方向（外向き）を指す
            //     → head:"start" と同じ挙動（from=end, to=start）。
            //   - orient="auto"（非慣用。marker-end 用の慣用形をそのまま
            //     marker-start に流用した形）: パス方向そのままに向けるため、
            //     矢頭は start→end 方向（内向き）を指す → 実質 head:"end"
            //     と同じ挙動（from=start, to=end）。
            //   - それ以外（固定角度、または orient 省略時の既定値 "0"）:
            //     矢頭の向きはパス方向と無関係な固定角度であり、start/end
            //     座標だけからは視覚上の向きを判定できない。黙って推測せず
            //     エラーにする（呼び出し側は head:"<selector>" を使うこと）。
            var startOrient = resolveMarkerOrient(markerStartVal);
            if (startOrient === 'auto-start-reverse') {
              from = end; to = start;
              headNote = 'auto: marker-start=' + markerStartVal + '（orient="auto-start-reverse"） を検出 → head="start" として判定; ';
            } else if (startOrient === 'auto') {
              from = start; to = end;
              headNote = 'auto: marker-start=' + markerStartVal + '（orient="auto"、auto-start-reverse ではない非慣用形） を検出 → 矢頭はパス方向(start→end)を向くため head="end" 相当として判定; ';
            } else {
              throw new Error(
                'arrow_direction: head="auto": marker-start=' + markerStartVal +
                ' の orient="' + (startOrient === null ? '(unresolved)' : startOrient) +
                '" は "auto"/"auto-start-reverse" のいずれでもなく、矢頭の向きが start/end 座標に依存しないため自動判定できません。head:"<selector>" で矢頭を描く要素を明示してください。'
              );
            }
          } else if (!endHas && !startHas) {
            from = start; to = end;
            headNote = 'auto fallback: marker が無いため end とみなした; ';
          } else {
            from = start; to = end;
            headNote = 'auto fallback: marker が両端にあるため end とみなした; ';
          }
        } else if (typeof head === 'string') {
          var headFound = selectBBox(head);
          if (!headFound.bbox) throw new Error('arrow_direction: head selector "' + head + '": ' + headFound.note);
          var dStart = Math.hypot(headFound.bbox.cx - start.x, headFound.bbox.cy - start.y);
          var dEnd = Math.hypot(headFound.bbox.cx - end.x, headFound.bbox.cy - end.y);
          if (dStart <= dEnd) {
            to = start; from = end;
          } else {
            to = end; from = start;
          }
          headNote = 'head="' + head + '" nearer to ' + (dStart <= dEnd ? 'start' : 'end') +
            ' (dist-to-start=' + dStart.toFixed(1) + ', dist-to-end=' + dEnd.toFixed(1) + '); ';
        } else {
          throw new Error('arrow_direction: head must be "start", "end", or a selector string');
        }

        var dx = to.x - from.x;
        var dy = to.y - from.y;
        var pass;
        if (a.direction === 'up' || a.direction === 'down') {
          pass = Math.abs(dy) >= Math.abs(dx) && (a.direction === 'down' ? dy > 0 : dy < 0);
        } else {
          pass = Math.abs(dx) >= Math.abs(dy) && (a.direction === 'right' ? dx > 0 : dx < 0);
        }
        return {
          pass: pass,
          actual: { dx: dx, dy: dy, head: head },
          expected: a.direction,
          message: (headNote || '') + 'from=(' + from.x.toFixed(1) + ',' + from.y.toFixed(1) + ') to=(' + to.x.toFixed(1) + ',' + to.y.toFixed(1) + ') dx=' + dx.toFixed(1) + ' dy=' + dy.toFixed(1) + ' vs "' + a.direction + '"',
        };
      },
    },

    compare: {
      required: ['left', 'op', 'right'],
      run: function (a) {
        var validOps = ['lt', 'le', 'gt', 'ge', 'eq'];
        if (validOps.indexOf(a.op) === -1) {
          throw new Error('compare: unknown op "' + a.op + '" (allowed: ' + validOps.join(', ') + ')');
        }
        var eps = typeof a.eps === 'number' ? a.eps : 0;
        var L = resolveOperand(a.left);
        var R = resolveOperand(a.right);
        var pass;
        switch (a.op) {
          case 'lt': pass = L.value < R.value; break;
          case 'le': pass = L.value <= R.value + eps; break;
          case 'gt': pass = L.value > R.value; break;
          case 'ge': pass = L.value >= R.value - eps; break;
          case 'eq': pass = Math.abs(L.value - R.value) <= eps; break;
        }
        return {
          pass: pass,
          actual: L.value,
          expected: a.op + ' ' + R.value + ' (eps=' + eps + ')',
          message: 'left=' + L.value + ' ' + a.op + ' right=' + R.value + ' (eps=' + eps + ')',
        };
      },
    },
  };

  function evalAssertion(a, id) {
    var type = a && a.type;
    var handler = HANDLERS[type];
    if (!handler) {
      return { id: id, type: type, pass: false, actual: null, expected: null, message: 'unknown assertion type: ' + type };
    }
    var missing = handler.required.filter(function (k) {
      return a[k] === undefined || a[k] === null;
    });
    if (missing.length) {
      return { id: id, type: type, pass: false, actual: null, expected: null, message: 'missing required field(s): ' + missing.join(', ') };
    }
    var r;
    try {
      r = handler.run(a);
    } catch (e) {
      r = { pass: false, actual: null, expected: null, message: 'error: ' + (e && e.message ? e.message : String(e)) };
    }
    return {
      id: id,
      type: type,
      pass: !!r.pass,
      actual: r.actual === undefined ? null : r.actual,
      expected: r.expected === undefined ? null : r.expected,
      message: r.message || (r.pass ? 'ok' : 'failed'),
    };
  }

  function runAssertions(spec) {
    try {
      var list = spec && Array.isArray(spec.assertions) ? spec.assertions : [];
      var results = [];
      var seenIds = {};
      for (var i = 0; i < list.length; i++) {
        var a = list[i] || {};
        var id = typeof a.id === 'string' && a.id.length > 0 ? a.id : ('assertion_' + i);
        var result;
        if (seenIds[id]) {
          result = { id: id, type: a.type, pass: false, actual: null, expected: null, message: 'duplicate assertion id: ' + id };
        } else {
          seenIds[id] = true;
          try {
            result = evalAssertion(a, id);
          } catch (e) {
            result = { id: id, type: a.type, pass: false, actual: null, expected: null, message: 'internal error: ' + (e && e.message ? e.message : String(e)) };
          }
        }
        results.push(result);
      }
      var passCount = results.filter(function (r) { return r.pass; }).length;
      var failCount = results.length - passCount;
      return { results: results, pass_count: passCount, fail_count: failCount };
    } catch (fatal) {
      return {
        results: [{ id: 'runAssertions', type: 'internal', pass: false, actual: null, expected: null, message: 'fatal error in runAssertions: ' + (fatal && fatal.message ? fatal.message : String(fatal)) }],
        pass_count: 0,
        fail_count: 1,
      };
    }
  }

  global.__fablizeGeometry = {
    bboxOf: bboxOf,
    selectFirst: selectFirst,
    runAssertions: runAssertions,
  };
})(window);

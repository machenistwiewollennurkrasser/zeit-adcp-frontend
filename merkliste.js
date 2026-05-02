/**
 * merkliste.js
 * ZEIT AdCP Merklisten-System - Storage-Layer v1
 *
 * Globale API: window.Merkliste
 *   getItems()                      -> Item[]
 *   addItem(data)                   -> item_id | null (null = Duplikat)
 *   removeItem(item_id)             -> boolean
 *   clearAll()                      -> void
 *   isAdded(product_id, format_id, date) -> boolean
 *   getGlobalPriceTier()            -> string
 *   setGlobalPriceTier(tier)        -> void
 *   subscribe(callback)             -> unsubscribe-Funktion
 *   unsubscribe(callback)           -> void
 *
 * Item-Schema (alle Felder optional ausser item_id, product_id, format_id):
 *   item_id           string  (auto)
 *   added_at          string  ISO 8601 (auto)
 *   product_id        string
 *   product_name      string
 *   product_subtitle  string
 *   product_type      string
 *   format_id         string
 *   format_name       string
 *   schedule          { type: 'date'|'kw'|'none', date, label, ad_close_label }
 *   pricing           { list_price, list_price_label, price_locked, locked_tier,
 *                       locked_price, locked_label, currency }
 *   cluster           { cluster_id, cluster_display_name }
 *   note              string | null
 */

window.Merkliste = (() => {

  // ============================================================
  // Konstanten
  // ============================================================

  const STORAGE_KEY     = 'zeit_adcp_merkliste_v1';
  const SCHEMA_VERSION  = 1;
  const DEFAULT_TIER    = 'listenpreis';

  // ============================================================
  // Interner State
  // ============================================================

  let _state = _emptyState();
  let _subscribers = [];

  function _emptyState() {
    return { version: SCHEMA_VERSION, items: [], globalPriceTier: DEFAULT_TIER };
  }

  // ============================================================
  // Persistence
  // ============================================================

  function _load() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return;
      const parsed = JSON.parse(raw);
      if (!parsed || parsed.version !== SCHEMA_VERSION) {
        // Schema-Mismatch: sauber zuruecksetzen
        _state = _emptyState();
        _persist();
        return;
      }
      _state = parsed;
      if (!Array.isArray(_state.items)) _state.items = [];
      if (!_state.globalPriceTier)      _state.globalPriceTier = DEFAULT_TIER;
    } catch (e) {
      _state = _emptyState();
    }
  }

  function _persist() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(_state));
    } catch (e) {
      // localStorage nicht verfuegbar (z.B. Private Mode mit Sperrung)
      console.warn('Merkliste: localStorage nicht beschreibbar', e);
    }
  }

  // ============================================================
  // Observer
  // ============================================================

  function _notify() {
    const snapshot = getItems();
    _subscribers.forEach(cb => {
      try { cb(snapshot); } catch (e) {}
    });
  }

  function subscribe(callback) {
    if (typeof callback !== 'function') return () => {};
    _subscribers.push(callback);
    return () => unsubscribe(callback);
  }

  function unsubscribe(callback) {
    _subscribers = _subscribers.filter(cb => cb !== callback);
  }

  // ============================================================
  // Hilfsfunktionen
  // ============================================================

  function _makeId() {
    return Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
  }

  function _normalizeSchedule(sched) {
    if (!sched || typeof sched !== 'object') {
      return { type: 'none', date: null, label: null, ad_close_label: null };
    }
    return {
      type:           sched.type          || 'none',
      date:           sched.date          || null,
      label:          sched.label         || null,
      ad_close_label: sched.ad_close_label || null,
    };
  }

  function _normalizePricing(pricing) {
    if (!pricing || typeof pricing !== 'object') {
      return {
        list_price: null, list_price_label: null,
        price_locked: false, locked_tier: null, locked_price: null, locked_label: null,
        currency: 'EUR',
      };
    }
    return {
      list_price:       pricing.list_price       ?? null,
      list_price_label: pricing.list_price_label ?? null,
      price_locked:     pricing.price_locked      ? true : false,
      locked_tier:      pricing.locked_tier       ?? null,
      locked_price:     pricing.locked_price      ?? null,
      locked_label:     pricing.locked_label      ?? null,
      currency:         'EUR',
    };
  }

  function _normalizeCluster(cluster) {
    if (!cluster || typeof cluster !== 'object') {
      return { cluster_id: null, cluster_display_name: null };
    }
    return {
      cluster_id:           cluster.cluster_id           ?? null,
      cluster_display_name: cluster.cluster_display_name ?? null,
    };
  }

  // ============================================================
  // Duplicate-Detection: product_id + format_id + schedule.date
  // ============================================================

  function _duplicateKey(product_id, format_id, date) {
    return `${product_id}|${format_id}|${date || ''}`;
  }

  function _existingKeys() {
    return new Set(_state.items.map(x =>
      _duplicateKey(x.product_id, x.format_id, x.schedule?.date || null)
    ));
  }

  function isAdded(product_id, format_id, date) {
    return _existingKeys().has(_duplicateKey(product_id, format_id, date || null));
  }

  // ============================================================
  // Public API
  // ============================================================

  function getItems() {
    // Gibt shallow copy zurueck (Items selbst sind unveraendert)
    return _state.items.slice();
  }

  function addItem(data) {
    const pid      = data.product_id  || '';
    const fid      = data.format_id   || '';
    const schedDate = data.schedule?.date || null;

    if (isAdded(pid, fid, schedDate)) return null;

    const item = {
      item_id:          _makeId(),
      added_at:         new Date().toISOString(),
      product_id:       pid,
      product_name:     data.product_name     || '',
      product_subtitle: data.product_subtitle || '',
      product_type:     data.product_type     || '',
      format_id:        fid,
      format_name:      data.format_name      || '',
      schedule:         _normalizeSchedule(data.schedule),
      pricing:          _normalizePricing(data.pricing),
      cluster:          _normalizeCluster(data.cluster),
      note:             data.note || null,
    };

    _state.items.push(item);
    _persist();
    _notify();
    return item.item_id;
  }

  function removeItem(item_id) {
    const len = _state.items.length;
    _state.items = _state.items.filter(x => x.item_id !== item_id);
    if (_state.items.length === len) return false;
    _persist();
    _notify();
    return true;
  }

  function clearAll() {
    _state.items = [];
    _persist();
    _notify();
  }

  function getGlobalPriceTier() {
    return _state.globalPriceTier || DEFAULT_TIER;
  }

  function setGlobalPriceTier(tier) {
    _state.globalPriceTier = tier;
    _persist();
    _notify();
  }

  // ============================================================
  // Init
  // ============================================================

  _load();

  return {
    getItems,
    addItem,
    removeItem,
    clearAll,
    isAdded,
    getGlobalPriceTier,
    setGlobalPriceTier,
    subscribe,
    unsubscribe,
  };

})();

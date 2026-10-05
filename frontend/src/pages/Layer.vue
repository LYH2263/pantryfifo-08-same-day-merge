<template>
  <div>
    <h1>{{ props.layer }} 层</h1>
    <span v-for="x in rows" :key="x.id" class="lot">
      #{{ x.id }} {{ x.name }} ×{{ x.qty_remain }} · {{ x.expiry }}
      <em v-if="x.data_quality === 'dirty'" class="dirty">脏批</em>
    </span>
  </div>
</template>
<script setup>
import { ref, watch, onMounted } from 'vue'
import { api } from '../api'
const props = defineProps({ layer: String })
const rows = ref([])
async function load() { rows.value = await api('/fridge?layer=' + props.layer) }
watch(() => props.layer, load)
onMounted(load)
</script>

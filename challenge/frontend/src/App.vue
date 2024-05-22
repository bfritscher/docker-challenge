<script setup>
import { ref } from 'vue'
const isFormVisible = ref(false)
const entries = ref([])
const entry = ref({
  name: '',
  email: '',
  message: ''
})

function getEntries() {
  fetch('/entries')
    .then(response => response.json())
    .then(data => entries.value = data)
}
getEntries()

function sign() {
  fetch('/entry', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json'
    },
    body: JSON.stringify(entry.value)
  })
  .then(response => {
    if (response.status === 204) {
      entry.value = {
        name: '',
        email: '',
        message: ''
      }
      isFormVisible.value = false
      getEntries()
    }
  })
}
</script>

<template>
    <h2>Guestbook</h2>
    <h3><a href="#" @click="isFormVisible = true"><img src="./assets/sign_here.png" alt="sign here"></a></h3>
    <div v-if="isFormVisible" @submit.prevent="sign()">
      <h2>Sign Guestbook</h2>
      <form class="box">
          <label><span>Name:</span><input type="text" name="name" required v-model="entry.name" /></label>
          <label><span>Email:</span><input type="email" name="email" v-model="entry.email" /></label>
          <label><span>Message:</span><textarea name="message" required v-model="entry.message"></textarea></label>
          <label><span></span><input type="submit" value="Sign" /></label>
      </form>
    </div>
    <h3>Entries</h3>
    <section class="entry box" v-for="entry in entries">
      <pre>{{ entry.message }}</pre>
      <header>
        {{ entry.name }} <span v-if="entry.email">&lt;{{ entry.email }}&gt;</span>
        <em>signed on {{ entry.signed_on }}</em>
      </header>
    </section>
</template>
